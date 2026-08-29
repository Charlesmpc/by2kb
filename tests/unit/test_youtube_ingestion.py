from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from by2kb.config import Config, LlmConfig, SourceConfig
from by2kb.jobs.runner import ingest_url
from by2kb.providers.asr import AsrOptions, AsrResult
from by2kb.providers.asr_registry import AsrProviderRegistry
from by2kb.providers.source_registry import SourceProviderRegistry
from by2kb.providers.yt_dlp_source import YtDlpSourceConfig, YtDlpSourceProvider


class YoutubeBackend:
    version = "2026.08-test"

    def __init__(self, tmp_path: Path, *, captioned: bool):
        self.tmp_path = tmp_path
        self.captioned = captioned
        self.downloads = 0

    def extract(self, _url: str, options: dict):
        if options.get("_download"):
            self.downloads += 1
            audio = self.tmp_path / "source.webm"
            audio.write_bytes(b"youtube-audio")
            requested = [{"filepath": str(audio)}]
        else:
            requested = []
        subtitles = (
            {"en": [{"ext": "json3", "url": "https://captions.test/en.json3"}]}
            if self.captioned
            else {}
        )
        return {
            "id": "video123",
            "title": "YouTube knowledge test",
            "channel": "by2kb fixtures",
            "duration": 12,
            "extractor": "youtube",
            "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=video123",
            "subtitles": subtitles,
            "automatic_captions": {},
            "requested_downloads": requested,
        }


class RecordingAsr:
    name = "configured_asr"

    def __init__(self):
        self.calls = 0

    async def transcribe(self, _audio, _options: AsrOptions) -> AsrResult:
        self.calls += 1
        return AsrResult(
            provider=self.name,
            model="configured-model",
            language="en",
            text="",
            segments=[
                {
                    "start": index * 2,
                    "end": (index + 1) * 2,
                    "text": f"Distinct configured ASR knowledge segment number {index}.",
                }
                for index in range(6)
            ],
            provenance={"runtime": "fixture"},
        )


def _source_registry(backend: YoutubeBackend) -> SourceProviderRegistry:
    registry = SourceProviderRegistry()
    registry.register(
        "yt_dlp",
        YtDlpSourceProvider(YtDlpSourceConfig(), backend=backend),
    )
    return registry


def _config(tmp_path: Path, *, enricher: str = "api") -> Config:
    return Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "home" / "jobs.db",
        asr_provider="configured_asr",
        enrichment_executor=enricher,
        llm=LlmConfig(
            api_key="fixture-key",
            base_url="https://llm.test/v1",
            model="fixture-model",
        ),
        sources=SourceConfig(providers=["yt_dlp"]),
    )


def _caption_payload() -> dict:
    return {
        "events": [
            {
                "tStartMs": index * 2000,
                "dDurationMs": 2000,
                "segs": [
                    {
                        "utf8": (
                            f"Distinct caption knowledge segment number {index} "
                            "with useful evidence."
                        )
                    }
                ],
            }
            for index in range(6)
        ]
    }


def _transport(request: httpx.Request) -> httpx.Response:
    if request.url.host == "captions.test":
        return httpx.Response(200, json=_caption_payload())
    if request.url.host == "llm.test":
        payload = json.loads(request.content)
        prompt = payload["messages"][1]["content"]
        if "short-video-abstract" in prompt:
            body = "# Short abstract\n\nThis fixture is worth reading."
        else:
            body = "# Deep study notes\n\nA grounded knowledge map from the fixture."
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})
    raise AssertionError(f"unexpected request: {request.url}")


def _patch_http_client(monkeypatch):
    real_client = httpx.AsyncClient

    def factory(*_args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(_transport), **kwargs)

    monkeypatch.setattr("by2kb.jobs.runner.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_captioned_youtube_completes_all_artifacts_without_asr(
    tmp_path, monkeypatch
):
    _patch_http_client(monkeypatch)
    backend = YoutubeBackend(tmp_path, captioned=True)
    asr = RecordingAsr()
    asr_registry = AsrProviderRegistry()
    asr_registry.register(asr.name, lambda _client: asr)

    outcome = await ingest_url(
        "https://youtu.be/video123",
        _config(tmp_path),
        source_registry=_source_registry(backend),
        asr_registry=asr_registry,
    )

    assert outcome.status == "completed"
    assert asr.calls == 0
    assert backend.downloads == 0
    assert {"raw_md", "abstract_md", "updated_md"}.issubset(outcome.artifacts)
    transcript = json.loads(Path(outcome.artifacts["transcript_json"]).read_text())
    source = json.loads(Path(outcome.artifacts["source_json"]).read_text())
    assert transcript["source"]["platform"] == "youtube"
    assert transcript["transcript"]["kind"] == "human"
    assert source["provenance"]["route"] == "subtitle"
    assert source["provenance"]["provider_version"] == "2026.08-test"
    assert Path(outcome.artifacts["raw_md"]).parent.name == "video123"


@pytest.mark.asyncio
async def test_youtube_without_caption_uses_configured_asr_and_completes(
    tmp_path, monkeypatch
):
    _patch_http_client(monkeypatch)
    backend = YoutubeBackend(tmp_path, captioned=False)
    asr = RecordingAsr()
    asr_registry = AsrProviderRegistry()
    asr_registry.register(asr.name, lambda _client: asr)

    outcome = await ingest_url(
        "https://www.youtube.com/watch?v=video123",
        _config(tmp_path),
        source_registry=_source_registry(backend),
        asr_registry=asr_registry,
    )

    assert outcome.status == "completed"
    assert backend.downloads == 1
    assert asr.calls == 1
    source = json.loads(Path(outcome.artifacts["source_json"]).read_text())
    transcript = json.loads(Path(outcome.artifacts["transcript_json"]).read_text())
    assert source["provenance"]["route"] == "audio_fallback"
    assert source["asr_provider"] == "configured_asr"
    assert transcript["transcript"]["kind"] == "asr"


@pytest.mark.asyncio
async def test_youtube_url_variants_have_one_stable_identity(tmp_path):
    backend = YoutubeBackend(tmp_path, captioned=True)
    provider = YtDlpSourceProvider(YtDlpSourceConfig(), backend=backend)
    async with httpx.AsyncClient() as client:
        short = await provider.resolve("https://youtu.be/video123", client)
        canonical = await provider.resolve(
            "https://www.youtube.com/watch?v=video123", client
        )
        shorts = await provider.resolve(
            "https://www.youtube.com/shorts/video123", client
        )

    assert {(item.platform, item.video_id) for item in (short, canonical, shorts)} == {
        ("youtube", "video123")
    }
