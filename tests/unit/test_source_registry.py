from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from by2kb.config import load_config
from by2kb.errors import ConfigError, NeedsAuth, RateLimited, UnsupportedUrl
from by2kb.providers.base import FetchOptions, SourceIdentity
from by2kb.providers.source_bilibili import BilibiliSourceProvider
from by2kb.providers.source_registry import SourceProviderRegistry
from by2kb.providers.yt_dlp_source import (
    YtDlpSourceConfig,
    YtDlpSourceProvider,
    _mapped_error,
)


class FakeProvider:
    def __init__(self, name: str, supported: bool = True):
        self.name = name
        self.supported = supported

    def supports(self, _source: str) -> bool:
        return self.supported


class FakeYtDlpBackend:
    version = "test-2026.08"

    def __init__(self, info: dict, *, audio_path: Path | None = None):
        self.info = info
        self.audio_path = audio_path
        self.calls: list[tuple[str, dict]] = []

    def extract(self, url: str, options: dict):
        self.calls.append((url, options))
        if options.get("_download"):
            assert self.audio_path is not None
            self.audio_path.write_bytes(b"audio")
            return {**self.info, "requested_downloads": [{"filepath": str(self.audio_path)}]}
        return dict(self.info)


def _youtube_info(**overrides):
    result = {
        "id": "dQw4w9WgXcQ",
        "title": "Provider test",
        "channel": "by2kb",
        "duration": 12,
        "extractor": "youtube",
        "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "subtitles": {},
        "automatic_captions": {},
    }
    result.update(overrides)
    return result


def test_registry_uses_configured_order_not_registration_order():
    registry = SourceProviderRegistry()
    registry.register("native", FakeProvider("native"))
    registry.register("generic", FakeProvider("generic"))

    selected = registry.select("https://example.test/video", ["generic", "native"])

    assert selected.name == "generic"


def test_registry_skips_provider_that_does_not_support_source():
    registry = SourceProviderRegistry()
    registry.register("first", FakeProvider("first", supported=False))
    registry.register("second", FakeProvider("second"))

    assert registry.select("source", ["first", "second"]).name == "second"


def test_registry_rejects_unknown_configured_provider():
    registry = SourceProviderRegistry()
    registry.register("native", FakeProvider("native"))

    with pytest.raises(ConfigError, match="available providers: native"):
        registry.select("source", ["missing"])


def test_native_bilibili_is_narrower_than_generic_ytdlp():
    native = BilibiliSourceProvider()
    generic = YtDlpSourceProvider(YtDlpSourceConfig(), backend=FakeYtDlpBackend({}))

    assert native.supports("https://www.bilibili.com/video/BV1jmbD65EP2/")
    assert not native.supports("https://youtu.be/dQw4w9WgXcQ")
    assert generic.supports("https://youtu.be/dQw4w9WgXcQ")


@pytest.mark.asyncio
async def test_ytdlp_prepares_manual_caption_without_audio(tmp_path):
    info = _youtube_info(
        subtitles={
            "en": [
                {
                    "ext": "json3",
                    "url": "https://captions.test/en.json3",
                }
            ]
        }
    )
    backend = FakeYtDlpBackend(info)
    provider = YtDlpSourceProvider(YtDlpSourceConfig(), backend=backend)
    transcript_payload = {
        "events": [
            {
                "tStartMs": 1000,
                "dDurationMs": 2000,
                "segs": [{"utf8": "Hello "}, {"utf8": "world"}],
            }
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(transcript_payload))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await provider.resolve(info["webpage_url"], client)
        stages = []
        prepared = await provider.prepare(
            identity,
            client,
            tmp_path,
            FetchOptions(preferred_languages=["en"]),
            set_stage=stages.append,
            cancel_check=lambda: None,
        )

    assert identity.platform == "youtube"
    assert prepared.audio is None
    assert prepared.transcript.transcript.kind == "human"
    assert prepared.transcript.transcript.segments[0].text == "Hello world"
    assert prepared.source_payload["provenance"]["route"] == "subtitle"
    assert stages == ["fetching_transcript"]
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_ytdlp_falls_back_to_audio_when_caption_is_missing(tmp_path):
    audio = tmp_path / "source.webm"
    backend = FakeYtDlpBackend(_youtube_info(), audio_path=audio)
    provider = YtDlpSourceProvider(YtDlpSourceConfig(), backend=backend)

    async with httpx.AsyncClient() as client:
        identity = await provider.resolve("https://youtu.be/dQw4w9WgXcQ", client)
        stages = []
        prepared = await provider.prepare(
            identity,
            client,
            tmp_path,
            FetchOptions(preferred_languages=["zh"]),
            set_stage=stages.append,
            cancel_check=lambda: None,
        )

    assert prepared.transcript is None
    assert prepared.audio.path == audio
    assert prepared.source_payload["provenance"]["route"] == "audio_fallback"
    assert stages == ["fetching_transcript", "capturing_media"]
    assert backend.calls[-1][1]["_download"] is True


def test_ytdlp_rejects_ambiguous_cookie_configuration():
    with pytest.raises(ConfigError, match="only one"):
        YtDlpSourceConfig.from_mapping(
            {"cookie_file": "cookies.txt", "cookies_from_browser": "chrome"}
        )


@pytest.mark.asyncio
async def test_ytdlp_rejects_playlist_metadata(tmp_path):
    backend = FakeYtDlpBackend(
        {"_type": "playlist", "id": "playlist", "entries": []}
    )
    provider = YtDlpSourceProvider(YtDlpSourceConfig(), backend=backend)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UnsupportedUrl, match="one video URL"):
            await provider.resolve("https://example.test/playlist", client)


def test_ytdlp_maps_auth_and_rate_limit_failures_to_stable_categories():
    assert isinstance(_mapped_error(Exception("Sign in to confirm")), NeedsAuth)
    assert isinstance(_mapped_error(Exception("HTTP Error 429")), RateLimited)


def test_source_provider_order_and_options_load_from_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        """[sources]
providers = ["bilibili_native", "yt_dlp"]

[sources.yt_dlp]
subtitle_policy = "manual_only"
playlist_policy = "reject"
""",
        encoding="utf-8",
    )

    config = load_config(home)

    assert config.sources.providers == ["bilibili_native", "yt_dlp"]
    assert config.sources.options["yt_dlp"]["subtitle_policy"] == "manual_only"
