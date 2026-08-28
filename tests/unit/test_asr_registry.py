from __future__ import annotations

import json
from pathlib import Path

import pytest

from by2kb.config import Config
from by2kb.errors import ConfigError
from by2kb.jobs import runner
from by2kb.providers.asr import AsrOptions, AsrResult
from by2kb.providers.asr_registry import AsrProviderRegistry
from by2kb.providers.base import LocalAudio, SourceIdentity
from by2kb.providers.bilibili import BilibiliVideoInfo


class FakeAsrProvider:
    def __init__(self, name: str):
        self.name = name

    async def transcribe(
        self, audio: LocalAudio, options: AsrOptions
    ) -> AsrResult:
        return AsrResult(provider=self.name, model="fake", text="ok")


def _factory(name: str):
    return lambda _client: FakeAsrProvider(name)


def _unavailable(message: str):
    def factory(_client):
        raise ConfigError(message)

    return factory


def test_explicit_provider_selection():
    registry = AsrProviderRegistry()
    registry.register("cloud", _factory("cloud"), priority=10)
    registry.register("local", _factory("local"), priority=20)

    provider = registry.create("CLOUD", object())  # type: ignore[arg-type]

    assert provider.name == "cloud"
    assert registry.names == ("local", "cloud")


def test_auto_skips_unavailable_provider():
    registry = AsrProviderRegistry()

    registry.register(
        "preferred", _unavailable("optional dependency is missing"), priority=20
    )
    registry.register("fallback", _factory("fallback"), priority=10)

    provider = registry.create("auto", object())  # type: ignore[arg-type]

    assert provider.name == "fallback"


def test_auto_reports_all_unavailable_providers():
    registry = AsrProviderRegistry()
    registry.register("first", _unavailable("missing first"))
    registry.register("second", _unavailable("missing second"))

    with pytest.raises(ConfigError, match="first: missing first") as error:
        registry.create("auto", object())  # type: ignore[arg-type]

    assert "second: missing second" in str(error.value)


def test_unknown_provider_lists_available_names():
    registry = AsrProviderRegistry()
    registry.register("doubao_auc", _factory("doubao_auc"))

    with pytest.raises(ConfigError, match="available providers: doubao_auc"):
        registry.create("missing", object())  # type: ignore[arg-type]


def test_duplicate_and_reserved_names_are_rejected():
    registry = AsrProviderRegistry()
    registry.register("local", _factory("local"))

    with pytest.raises(ConfigError, match="already registered"):
        registry.register("local", _factory("local"))
    with pytest.raises(ConfigError, match="reserved"):
        registry.register("auto", _factory("auto"))


def test_factory_name_must_match_registration():
    registry = AsrProviderRegistry()
    registry.register("expected", _factory("different"))

    with pytest.raises(ConfigError, match="name mismatch"):
        registry.create("expected", object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ingest_uses_registry_and_persists_provider_identity(
    tmp_path, monkeypatch
):
    identity = SourceIdentity(
        platform="bilibili",
        video_id="BV1registry1",
        canonical_url="https://www.bilibili.com/video/BV1registry1/",
    )
    audio_path = tmp_path / "audio.m4a"
    audio_path.write_bytes(b"audio")

    async def fake_resolve_url(_url, _client):
        return identity

    async def fake_fetch_video_info(_client, _video_id):
        return BilibiliVideoInfo(
            bvid=identity.video_id,
            aid=1,
            cid=2,
            title="Registry test",
            author="by2kb",
            duration_s=10,
        )

    class FakeMediaProvider:
        def __init__(self, *_args):
            pass

        async def fetch_audio(self, _identity, _options):
            return LocalAudio(
                path=audio_path,
                format="mp4",
                duration_s=10,
                size_bytes=audio_path.stat().st_size,
            )

    class RegistryProvider(FakeAsrProvider):
        async def transcribe(self, audio, options):
            return AsrResult(
                provider=self.name,
                model="registry-model",
                language="zh",
                text="测试文本",
                segments=[{"start": 0, "end": 10, "text": "测试文本"}],
                provenance={"runtime": "test"},
            )

    monkeypatch.setattr(runner, "resolve_url", fake_resolve_url)
    monkeypatch.setattr(runner.bilibili, "fetch_video_info", fake_fetch_video_info)
    monkeypatch.setattr(runner.bilibili, "BilibiliMediaProvider", FakeMediaProvider)

    registry = AsrProviderRegistry()
    registry.register(
        "registry_test", lambda _client: RegistryProvider("registry_test")
    )
    config = Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "home" / "jobs.db",
        asr_provider="registry_test",
        enrichment_executor="disabled",
    )

    outcome = await runner.ingest_url(
        identity.canonical_url,
        config,
        asr_registry=registry,
    )

    assert outcome.status == "completed"
    transcript = json.loads(
        Path(outcome.artifacts["transcript_json"]).read_text(encoding="utf-8")
    )
    assert transcript["transcript"]["provider"] == "registry_test"
    assert transcript["transcript"]["model"] == "registry-model"
    source = json.loads(
        Path(outcome.artifacts["source_json"]).read_text(encoding="utf-8")
    )
    assert source["asr_provider"] == "registry_test"
    assert source["asr_model"] == "registry-model"
    assert source["asr_provenance"] == {"runtime": "test"}
