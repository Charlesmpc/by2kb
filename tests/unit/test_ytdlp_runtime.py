import json
import sys
from importlib import metadata
from types import SimpleNamespace

import pytest

from by2kb.errors import ConfigError
from by2kb.providers import yt_dlp_runtime as runtime
from by2kb.providers.yt_dlp_source import YtDlpSourceConfig, YtDlpSourceProvider
from by2kb.search.providers import _YTDLP_WORKER, default_registry


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv("BY2KB_YTDLP_JS_RUNTIME", raising=False)
    monkeypatch.delenv("BY2KB_YTDLP_JS_RUNTIME_PATH", raising=False)


def fake_runtime(monkeypatch, versions):
    monkeypatch.setattr(runtime.shutil, "which", lambda name: name if name in versions else None)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=versions[command[0]])
    monkeypatch.setattr(runtime.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("versions,expected", [
    ({"deno": "deno 2.9.5\nv8 14.0", "node": "v24.18.0"}, "deno"),
    ({"node": "v22.0.0"}, "node"),
    ({"deno": "deno 2.2.0", "node": "v24.18.0"}, "node"),
    ({"node": "v20.0.0"}, None),
    ({}, None),
])
def test_auto_selects_a_supported_runtime(monkeypatch, versions, expected):
    calls = fake_runtime(monkeypatch, versions)
    status = runtime.runtime_status(runtime.JsSettings())
    assert status.name == expected
    assert all(kwargs["timeout"] == 3 and not kwargs.get("shell") for _, kwargs in calls)
    assert status.options()["js_runtimes"] == ({expected: {"path": expected}} if expected else {})


def test_explicit_path_and_environment_override(monkeypatch):
    fake_runtime(monkeypatch, {"/custom/node": "v24.18.0"})
    monkeypatch.setenv("BY2KB_YTDLP_JS_RUNTIME", "node")
    monkeypatch.setenv("BY2KB_YTDLP_JS_RUNTIME_PATH", "/custom/node")
    settings = runtime.JsSettings.from_mapping({"js_runtime": "deno"})
    assert runtime.js_options(settings) == {"js_runtimes": {"node": {"path": "/custom/node"}}}


def test_missing_explicit_runtime_fails_without_silent_fallback(monkeypatch):
    fake_runtime(monkeypatch, {"deno": "deno 2.9.5"})
    with pytest.raises(ConfigError, match="Configured YouTube JS runtime"):
        runtime.js_options(runtime.JsSettings("node", "/missing/node"))


@pytest.mark.parametrize("values", [{"js_runtime": "bash"}, {"js_runtime_path": "/custom/node"}])
def test_invalid_runtime_settings_are_rejected(values):
    with pytest.raises(ConfigError):
        YtDlpSourceConfig.from_mapping(values)


def test_runtime_can_be_explicitly_disabled(monkeypatch):
    def unexpected(*args):
        raise AssertionError("Must not probe disabled runtime")
    monkeypatch.setattr(runtime.shutil, "which", unexpected)
    assert runtime.js_options(runtime.JsSettings("none")) == {"js_runtimes": {}}


@pytest.mark.parametrize("installed,required,ok", [
    ("0.8.0", "==0.8.0", True), ("0.7.0", "==0.8.0", False),
    ("0.8.1", ">=0.8.0,<0.9", True), (None, "==0.8.0", False),
])
def test_ejs_matches_the_installed_ytdlp_requirement(monkeypatch, installed, required, ok):
    def version(name):
        if installed is None:
            raise metadata.PackageNotFoundError(name)
        return installed
    monkeypatch.setattr(runtime.metadata, "version", version)
    monkeypatch.setattr(runtime.metadata, "requires", lambda name: [f'yt-dlp-ejs{required}; extra == "default"'])
    assert runtime.ejs_status()[0] is ok


def test_ingestion_passes_js_options_for_metadata_and_download(monkeypatch, tmp_path):
    fake_runtime(monkeypatch, {"node": "v24.18.0"})
    provider = YtDlpSourceProvider(YtDlpSourceConfig())
    for download in (False, True):
        options = provider._options(download=download, work_dir=tmp_path)
        assert options["js_runtimes"] == {"node": {"path": "node"}}
        assert "cookiefile" not in options and "cookiesfrombrowser" not in options


@pytest.mark.parametrize("flat", [False, True])
def test_search_worker_enables_same_js_runtime_without_downloading(monkeypatch, capsys, flat):
    fake_runtime(monkeypatch, {"/custom/node": "v24.18.0"})
    captured = {}
    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, source, *, download):
            assert not download
            item = {"id": "video123", "title": "Test"}
            return {"entries": [item]} if flat else item
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(sys, "argv", ["worker", "public-source", "1" if flat else "0",
        json.dumps({"js_runtime": "node", "js_runtime_path": "/custom/node"})])
    # Test execution without changing pytest's capture stream encoding.
    exec(_YTDLP_WORKER.replace("sys.stdout.reconfigure(encoding='utf-8')", ""), {})
    json.loads(capsys.readouterr().out)
    assert captured["js_runtimes"] == {"node": {"path": "/custom/node"}}
    assert captured["skip_download"] and not captured["cachedir"]
    assert "cookiefile" not in captured and "cookiesfrombrowser" not in captured


def test_search_forwards_only_js_settings_not_authentication():
    registry = default_registry({"js_runtime": "node", "cookie_file": "private-cookie-file"})
    assert registry._providers["youtube"].js_settings == {"js_runtime": "node"}


def test_timeout_is_an_unavailable_runtime(monkeypatch):
    fake_runtime(monkeypatch, {"node": "v24.18.0"})
    def timeout(*args, **kwargs):
        raise runtime.subprocess.TimeoutExpired("node", 3)
    monkeypatch.setattr(runtime.subprocess, "run", timeout)
    assert not runtime.runtime_status(runtime.JsSettings()).name
