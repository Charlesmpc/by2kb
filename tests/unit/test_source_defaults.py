from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from by2kb.config import DEFAULT_SOURCE_PROVIDERS, SourceConfig, load_config
from by2kb.errors import ConfigError, UnsupportedUrl
from by2kb.providers.source_registry import build_default_source_registry
from by2kb.providers.yt_dlp_source import YtDlpBackend
from by2kb.setup import InitSettings


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Track the original value even if it was absent: load_env_file writes to
    # os.environ directly, so monkeypatch must restore that mutation at teardown.
    monkeypatch.setenv("BY2KB_SOURCE_PROVIDERS", "")
    for key in ("BY2KB_SOURCE_PROVIDERS", "BY2KB_ENV_FILE", "BY2KB_ASR_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BY2KB_HOME", str(tmp_path))


def _select(config, url):
    registry = build_default_source_registry(
        source_options=config.sources.options, home=config.home
    )
    return registry.select(url, config.sources.providers)


@pytest.mark.parametrize("content", [None, "", "[sources]\n", '''
# Existing deployment: only browser fallback and ASR were customized.
library_root = "/existing/knowledge"
[sources.fallback]
provider = "browser"
[sources.browser]
cdp_url = "http://127.0.0.1:9222"
[sources.yt_dlp]
subtitle_policy = "manual_only"
cookie_file = "/existing/cookies.txt"
[asr]
provider = "doubao_auc"
'''])
def test_missing_provider_list_inherits_defaults_without_rewriting(tmp_path, content):
    path = tmp_path / "config.toml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    env = tmp_path / ".env"
    env.write_bytes(b"# user-owned configuration\n")
    before = path.read_bytes() if path.exists() else None

    config = load_config(tmp_path)

    assert config.sources.providers == list(DEFAULT_SOURCE_PROVIDERS)
    assert _select(config, "https://youtu.be/video123").name == "yt_dlp"
    assert _select(config, "https://www.bilibili.com/video/BV1jmbD65EP2").name == "bilibili_native"
    assert (path.read_bytes() if path.exists() else None) == before
    assert env.read_bytes() == b"# user-owned configuration\n"
    if content and "doubao_auc" in content:
        assert config.asr_provider == "doubao_auc"
        assert config.library_root.as_posix() == "/existing/knowledge"
        assert config.sources.options["fallback"]["provider"] == "browser"
        assert config.sources.options["browser"]["cdp_url"] == "http://127.0.0.1:9222"
        assert config.sources.options["yt_dlp"]["subtitle_policy"] == "manual_only"
        assert config.sources.options["yt_dlp"]["cookie_file"] == "/existing/cookies.txt"


@pytest.mark.parametrize("providers", [
    '["bilibili_native"]', '["browser"]', '["yt_dlp", "bilibili_native"]', '[]',
])
def test_explicit_lists_remain_exact_overrides(tmp_path, providers):
    content = f"[sources]\nproviders = {providers}\n"
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    config = load_config(tmp_path)
    assert config.sources.providers == tomllib.loads(content)["sources"]["providers"]
    assert path.read_text(encoding="utf-8") == content
    if providers in ('["bilibili_native"]', '["browser"]'):
        with pytest.raises(UnsupportedUrl, match="provider-list override"):
            _select(config, "https://youtu.be/video123")
    elif providers == '[]':
        with pytest.raises(ConfigError, match="cannot be empty"):
            _select(config, "https://youtu.be/video123")
    else:
        assert _select(config, "https://www.bilibili.com/video/BV1jmbD65EP2").name == "yt_dlp"


def test_environment_override_wins_without_changing_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_bytes(b'[sources]\nproviders = ["browser"]\n')
    monkeypatch.setenv("BY2KB_SOURCE_PROVIDERS", " yt_dlp , bilibili_native ")
    assert load_config(tmp_path).sources.providers == ["yt_dlp", "bilibili_native"]
    assert path.read_bytes() == b'[sources]\nproviders = ["browser"]\n'


def test_dotenv_override_is_preserved(tmp_path):
    (tmp_path / ".env").write_bytes(b"BY2KB_SOURCE_PROVIDERS=bilibili_native\n")
    assert load_config(tmp_path).sources.providers == ["bilibili_native"]
    assert (tmp_path / ".env").read_bytes() == b"BY2KB_SOURCE_PROVIDERS=bilibili_native\n"


def test_explicit_disable_is_not_undone_by_defaults(tmp_path):
    (tmp_path / "config.toml").write_text('[sources.yt_dlp]\nenabled = false\n')
    config = load_config(tmp_path)
    with pytest.raises(UnsupportedUrl, match=r"sources.yt_dlp"):
        _select(config, "https://youtu.be/video123")
    assert _select(config, "https://www.bilibili.com/video/BV1jmbD65EP2").name == "bilibili_native"


def test_default_source_lists_are_independent_and_match_setup(tmp_path):
    first, second = SourceConfig(), SourceConfig()
    first.providers.clear()
    assert second.providers == list(DEFAULT_SOURCE_PROVIDERS)
    assert InitSettings(library_root=tmp_path).source_providers == DEFAULT_SOURCE_PROVIDERS


def test_missing_dependency_is_not_reported_as_a_provider_configuration_error(monkeypatch):
    def missing(_name):
        raise ImportError("fixture: yt-dlp missing")
    monkeypatch.setattr("by2kb.providers.yt_dlp_source.importlib.import_module", missing)
    with pytest.raises(ConfigError, match="installation is incomplete"):
        YtDlpBackend()._module()


def test_ytdlp_is_a_base_dependency_and_old_extras_still_exist():
    metadata = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    project = metadata["project"]
    assert any(dep.startswith("yt-dlp>=") for dep in project["dependencies"])
    assert {"youtube", "source-ytdlp"} <= project["optional-dependencies"].keys()
