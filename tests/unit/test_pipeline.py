import pytest

from by2kb.jobs.model import Job, JobStatus
from by2kb.jobs.store import JobStore
from by2kb.normalize import format_timestamp, from_asr_result
from by2kb.providers.asr import AsrResult
from by2kb.providers.base import SourceIdentity
from by2kb.writers.raw import render_raw_md, write_artifacts


@pytest.fixture
def identity():
    return SourceIdentity(
        platform="bilibili",
        video_id="BV1jmbD65EP2",
        canonical_url="https://www.bilibili.com/video/BV1jmbD65EP2/",
    )


def test_from_asr_result_text_only_becomes_single_segment(identity):
    result = AsrResult(
        provider="doubao_auc",
        model="bigmodel",
        text="你好，世界。",
        provenance={"staging": "private_tos_presigned_url"},
    )
    normalized = from_asr_result(
        identity,
        title="测试视频",
        author="测试作者",
        duration_ms=166000,
        asr_result=result,
        fetched_at="2026-08-17T00:00:00Z",
    )
    assert normalized.schema_version == 1
    assert normalized.source.platform == "bilibili"
    assert normalized.source.duration_ms == 166000
    assert normalized.transcript.kind == "asr"
    assert normalized.transcript.provider == "doubao_auc"
    assert len(normalized.transcript.segments) == 1
    assert normalized.transcript.segments[0].text == "你好，世界。"
    assert normalized.transcript.segments[0].duration_ms == 166000


def test_from_asr_result_with_timed_segments(identity):
    result = AsrResult(
        provider="doubao_auc",
        model="bigmodel",
        text="你好，世界。",
        segments=[
            {"start": 0.0, "end": 1.5, "text": "你好，"},
            {"start": 1.5, "end": 3.0, "text": "世界。"},
        ],
    )
    normalized = from_asr_result(
        identity,
        title="t",
        author="a",
        duration_ms=3000,
        asr_result=result,
        fetched_at="2026-08-17T00:00:00Z",
    )
    assert [s.start_ms for s in normalized.transcript.segments] == [0, 1500]
    assert [s.duration_ms for s in normalized.transcript.segments] == [1500, 1500]


def test_from_asr_result_empty_text(identity):
    result = AsrResult(provider="doubao_auc", model="bigmodel", text="")
    normalized = from_asr_result(
        identity,
        title="t",
        author="a",
        duration_ms=None,
        asr_result=result,
        fetched_at="2026-08-17T00:00:00Z",
    )
    assert normalized.transcript.segments == []


def test_format_timestamp():
    assert format_timestamp(0) == "0:00"
    assert format_timestamp(61_000) == "1:01"
    assert format_timestamp(786_500) == "13:06"


def test_render_raw_md_frontmatter_and_asr_note(identity):
    result = AsrResult(provider="doubao_auc", model="bigmodel", text="正文内容")
    normalized = from_asr_result(
        identity,
        title="测试:标题",
        author="作者",
        duration_ms=166000,
        asr_result=result,
        fetched_at="2026-08-17T00:00:00Z",
    )
    rendered = render_raw_md(normalized)
    assert rendered.startswith("---\n")
    assert "platform: bilibili" in rendered
    assert "video_id: BV1jmbD65EP2" in rendered
    assert 'title: "测试:标题"' in rendered
    assert "transcript_kind: asr" in rendered
    assert "ASR output without per-segment timing" in rendered
    assert "正文内容" in rendered


def test_render_raw_md_is_deterministic(identity):
    result = AsrResult(provider="doubao_auc", model="bigmodel", text="正文")
    kwargs = dict(
        title="t", author="a", duration_ms=1000, fetched_at="2026-08-17T00:00:00Z"
    )
    first = render_raw_md(
        from_asr_result(identity, asr_result=result, **kwargs)
    )
    second = render_raw_md(
        from_asr_result(identity, asr_result=result, **kwargs)
    )
    assert first == second


def test_write_artifacts_creates_three_files(tmp_path, identity):
    result = AsrResult(provider="doubao_auc", model="bigmodel", text="正文")
    normalized = from_asr_result(
        identity,
        title="t",
        author="a",
        duration_ms=1000,
        asr_result=result,
        fetched_at="2026-08-17T00:00:00Z",
    )
    outputs = write_artifacts(
        tmp_path, source_payload={"view": {"bvid": "BV1jmbD65EP2"}}, normalized=normalized
    )
    assert set(outputs) == {"source_json", "transcript_json", "raw_md"}
    assert outputs["source_json"].name == "source.json"
    assert outputs["transcript_json"].name == "transcript.json"
    assert outputs["raw_md"].name == "t-BV1jmbD65EP2.raw.md"
    for path in outputs.values():
        assert path.is_file() and path.stat().st_size > 0


def test_job_store_roundtrip_and_idempotent_lookup(tmp_path):
    store = JobStore(tmp_path / "by2kb.db")
    job = Job(id="abc123", platform="bilibili", video_id="BV1jmbD65EP2")
    store.create_job(job)

    found = store.find_existing("bilibili", "BV1jmbD65EP2")
    assert found is not None and found.id == "abc123"
    assert found.status == JobStatus.ACCEPTED

    store.update_status("abc123", JobStatus.COMPLETED)
    assert store.get_job("abc123").status == JobStatus.COMPLETED

    store.add_artifact("abc123", "raw_md", "/lib/raw.md", "hash123")
    artifacts = store.artifacts("abc123")
    assert artifacts[0]["kind"] == "raw_md"

    store.add_artifact("abc123", "raw_md", "/lib/new-raw.md", "hash456")
    artifacts = store.artifacts("abc123")
    assert len(artifacts) == 1
    assert artifacts[0]["path"] == "/lib/new-raw.md"
    store.close()
