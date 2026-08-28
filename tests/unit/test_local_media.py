from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from by2kb.config import Config, LlmConfig
from by2kb.errors import ConfigError, TerminalProviderError
from by2kb.jobs import runner
from by2kb.jobs.store import JobStore
from by2kb.providers import local_media
from by2kb.providers.asr import AsrOptions, AsrResult
from by2kb.providers.asr_registry import AsrProviderRegistry
from by2kb.providers.base import LocalAudio


class FixtureAsr:
    name = "fixture"

    async def transcribe(
        self, audio: LocalAudio, options: AsrOptions
    ) -> AsrResult:
        assert audio.path.is_file()
        return AsrResult(
            provider=self.name,
            model="fixture-model",
            language="en",
            text="Local media transcript.",
            segments=[
                {"start": 0.0, "end": 1.5, "text": "Local media transcript."}
            ],
            provenance={"runtime": "fixture"},
        )


def _registry() -> AsrProviderRegistry:
    registry = AsrProviderRegistry()
    registry.register("fixture", lambda _client: FixtureAsr())
    return registry


def _config(tmp_path: Path, *, executor: str = "disabled") -> Config:
    return Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "home" / "jobs.db",
        asr_provider="fixture",
        enrichment_executor=executor,
        llm=LlmConfig(api_key="key", model="model") if executor == "api" else LlmConfig(),
    )


async def _fake_prepare(info, work_dir):
    if info.media_kind == "video":
        audio_path = work_dir / "fixture.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"RIFF fixture audio")
        audio = LocalAudio(
            path=audio_path,
            format="wav",
            duration_s=1.5,
            size_bytes=audio_path.stat().st_size,
        )
    else:
        audio = LocalAudio(
            path=info.path,
            format=info.format,
            duration_s=1.5,
            size_bytes=info.size_bytes,
        )
    return audio, 1.5


def test_inspect_local_media_uses_content_identity_and_safe_provenance(tmp_path):
    first = tmp_path / "private-course.mp3"
    second = tmp_path / "renamed.mp3"
    first.write_bytes(b"same media content")
    second.write_bytes(first.read_bytes())

    left = local_media.inspect_local_media(first)
    right = local_media.inspect_local_media(second)
    payload = left.source_payload(duration_s=4.2)

    expected = hashlib.sha256(first.read_bytes()).hexdigest()
    assert left.identity.video_id == expected == right.identity.video_id
    assert left.identity.canonical_url == f"local://sha256/{expected}"
    assert payload["local_media"]["original_filename"] == first.name
    assert str(tmp_path) not in json.dumps(payload)


@pytest.mark.parametrize("name", ["missing.mp3", "unsupported.txt"])
def test_inspect_local_media_reports_actionable_errors(tmp_path, name):
    path = tmp_path / name
    if name == "unsupported.txt":
        path.write_text("not media", encoding="utf-8")
    expected = "not found" if name == "missing.mp3" else "unsupported local media type"

    with pytest.raises(TerminalProviderError, match=expected):
        local_media.inspect_local_media(path)


def test_inspect_local_media_rejects_empty_file(tmp_path):
    path = tmp_path / "empty.wav"
    path.touch()

    with pytest.raises(TerminalProviderError, match="is empty"):
        local_media.inspect_local_media(path)


@pytest.mark.asyncio
async def test_prepare_audio_probes_without_copying_source(tmp_path, monkeypatch):
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"RIFF audio")
    info = local_media.inspect_local_media(source)

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"format":{"duration":"12.5"}}', b""

    async def create_process(*args, **_kwargs):
        assert args[0] == "ffprobe"
        return Process()

    monkeypatch.setattr(local_media.asyncio, "create_subprocess_exec", create_process)
    audio, duration = await local_media.prepare_local_audio(info, tmp_path / "work")

    assert audio.path == source.resolve()
    assert duration == 12.5


@pytest.mark.asyncio
async def test_prepare_video_extracts_normalized_wav(tmp_path, monkeypatch):
    source = tmp_path / "lecture.mp4"
    source.write_bytes(b"ftyp video")
    info = local_media.inspect_local_media(source)
    calls = []

    class Process:
        returncode = 0

        def __init__(self, output=b"", error=b""):
            self.output = output
            self.error = error

        async def communicate(self):
            return self.output, self.error

    async def create_process(*args, **_kwargs):
        calls.append(args)
        if args[0] == "ffprobe":
            return Process(b'{"format":{"duration":"3.25"}}')
        output = Path(args[-1])
        output.write_bytes(b"RIFF extracted")
        return Process()

    monkeypatch.setattr(local_media.asyncio, "create_subprocess_exec", create_process)
    audio, duration = await local_media.prepare_local_audio(info, tmp_path / "work")

    assert [call[0] for call in calls] == ["ffprobe", "ffmpeg"]
    assert audio.path.name == "local-audio.wav"
    assert audio.format == "wav"
    assert duration == 3.25


@pytest.mark.asyncio
async def test_missing_ffprobe_has_install_guidance(tmp_path, monkeypatch):
    source = tmp_path / "meeting.mp3"
    source.write_bytes(b"ID3 audio")
    info = local_media.inspect_local_media(source)

    async def missing(*_args, **_kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(local_media.asyncio, "create_subprocess_exec", missing)
    with pytest.raises(ConfigError, match="install ffmpeg"):
        await local_media.prepare_local_audio(info, tmp_path / "work")


@pytest.mark.asyncio
async def test_local_audio_fixture_runs_raw_pipeline(tmp_path, monkeypatch):
    source = tmp_path / "meeting.mp3"
    source.write_bytes(b"ID3 fixture")
    monkeypatch.setattr(runner, "prepare_local_audio", _fake_prepare)

    outcome = await runner.ingest_source(
        str(source), _config(tmp_path), asr_registry=_registry()
    )

    assert outcome.status == "completed"
    transcript = json.loads(Path(outcome.artifacts["transcript_json"]).read_text())
    assert transcript["source"]["platform"] == "local"
    assert transcript["source"]["title"] == "meeting"
    assert transcript["transcript"]["quality"]["assessment_version"] == "1.0"
    assert transcript["transcript"]["quality"]["status"] == "pass"
    source_json = json.loads(Path(outcome.artifacts["source_json"]).read_text())
    assert source_json["local_media"]["original_filename"] == "meeting.mp3"
    assert "path" not in source_json["local_media"]
    assert source_json["transcript_quality"]["metrics"]["effective_char_count"] > 0


@pytest.mark.asyncio
async def test_local_video_fixture_runs_external_agent_pipeline(tmp_path, monkeypatch):
    source = tmp_path / "lecture.mp4"
    source.write_bytes(b"ftyp fixture video")
    monkeypatch.setattr(runner, "prepare_local_audio", _fake_prepare)

    outcome = await runner.ingest_source(
        str(source),
        _config(tmp_path, executor="external_agent"),
        asr_registry=_registry(),
    )

    assert outcome.status == "enrichment_pending"
    assert outcome.artifacts["raw_md"].endswith("raw.lecture.md")


@pytest.mark.asyncio
async def test_local_file_runs_direct_api_enrichment(tmp_path, monkeypatch):
    source = tmp_path / "podcast.flac"
    source.write_bytes(b"fLaC fixture")
    monkeypatch.setattr(runner, "prepare_local_audio", _fake_prepare)

    class RecordingLlm:
        provider = "fixture_llm"
        model = "fixture-model"

        async def complete(self, _system, user):
            return "# Short" if "short-video-abstract" in user else "# Long"

    monkeypatch.setattr(
        runner,
        "OpenAiCompatibleClient",
        lambda _config, _client: RecordingLlm(),
    )
    outcome = await runner.ingest_source(
        str(source),
        _config(tmp_path, executor="api"),
        asr_registry=_registry(),
    )

    assert outcome.status == "completed"
    assert set(outcome.artifacts) >= {"raw_md", "abstract_md", "updated_md"}


@pytest.mark.asyncio
async def test_identical_local_content_is_idempotent_across_filenames(
    tmp_path, monkeypatch
):
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"duplicate fixture")
    second.write_bytes(first.read_bytes())
    monkeypatch.setattr(runner, "prepare_local_audio", _fake_prepare)
    config = _config(tmp_path)

    completed = await runner.ingest_source(
        str(first), config, asr_registry=_registry()
    )
    duplicate = await runner.ingest_source(
        str(second), config, asr_registry=_registry()
    )

    assert completed.status == "completed"
    assert duplicate.status == "duplicate"
    assert duplicate.job_id == completed.job_id


@pytest.mark.asyncio
async def test_unusable_local_transcript_preserves_raw_but_skips_enrichment(
    tmp_path, monkeypatch
):
    source = tmp_path / "silent.wav"
    source.write_bytes(b"RIFF silent fixture")
    monkeypatch.setattr(runner, "prepare_local_audio", _fake_prepare)

    class EmptyAsr:
        name = "fixture"

        async def transcribe(self, _audio, _options):
            return AsrResult(provider=self.name, model="fixture", text="")

    registry = AsrProviderRegistry()
    registry.register("fixture", lambda _client: EmptyAsr())
    config = _config(tmp_path, executor="external_agent")
    outcome = await runner.ingest_source(
        str(source),
        config,
        asr_registry=registry,
    )

    assert outcome.status == "failed_terminal"
    assert "empty_transcript" in outcome.message
    assert Path(outcome.artifacts["raw_md"]).is_file()
    assert set(outcome.artifacts) == {"source_json", "transcript_json", "raw_md"}
    store = JobStore(config.db_path)
    try:
        assert store.get_enrichment_task(outcome.job_id) is None
    finally:
        store.close()
