from __future__ import annotations

import json
from pathlib import Path

import pytest

from by2kb.config import Config, LongFormConfig
from by2kb.enrichment import (
    ApiEnrichmentExecutor,
    create_enrichment_request,
    external_manifest,
)
from by2kb.longform import (
    KIND_ENRICHMENT_PLAN_JSON,
    LongFormEnrichmentPipeline,
    TranscriptChunkPlanner,
)
from by2kb.normalize import from_asr_result
from by2kb.providers.asr import AsrResult
from by2kb.providers.base import SourceIdentity


def _normalized(segment_count: int = 8, *, chars_per_segment: int = 40):
    identity = SourceIdentity(
        platform="test",
        video_id="long-form",
        canonical_url="test://long-form",
    )
    segments = []
    for index in range(segment_count):
        base = 0x4E00 + index * chars_per_segment
        text = "".join(chr(base + offset) for offset in range(chars_per_segment))
        segments.append(
            {
                "start": index * 60,
                "end": (index + 1) * 60,
                "text": text,
            }
        )
    return from_asr_result(
        identity,
        title="Long-form fixture",
        author="by2kb",
        duration_ms=segment_count * 60_000,
        asr_result=AsrResult(
            provider="fixture",
            model="fixture",
            text="".join(segment["text"] for segment in segments),
            segments=segments,
        ),
        fetched_at="2026-08-28T00:00:00Z",
    )


def _request(tmp_path: Path, long_form: LongFormConfig, *, segment_count: int = 8):
    raw = tmp_path / "raw.fixture.md"
    raw.write_text("# Raw\n\nfixture", encoding="utf-8")
    config = Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "home" / "jobs.db",
        long_form=long_form,
    )
    return create_enrichment_request(
        config,
        job_id="long-form-job",
        normalized=_normalized(segment_count),
        raw_path=raw,
        staging=tmp_path / "staging",
    )


class RecordingLlm:
    provider = "fixture"
    model = "fixture-model"

    def __init__(self, *, fail_at: int | None = None):
        self.calls = []
        self.fail_at = fail_at

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("fixture chunk failure")
        return f"Grounded summary {len(self.calls)}"


def test_chunk_planner_never_splits_normalized_segments(tmp_path):
    config = LongFormConfig(
        threshold_tokens=1,
        chunk_token_budget=70,
        chunk_duration_s=120,
        reduce_token_budget=100,
        reduce_group_size=2,
    )
    normalized = _normalized(6, chars_per_segment=40)

    plan = TranscriptChunkPlanner(config).plan(normalized)

    assert plan.strategy == "hierarchical"
    covered = []
    for chunk in plan.chunks:
        covered.extend(range(chunk.first_segment, chunk.last_segment + 1))
        assert chunk.first_segment <= chunk.last_segment
    assert covered == list(range(6))
    assert all(
        left.last_segment + 1 == right.first_segment
        for left, right in zip(plan.chunks, plan.chunks[1:], strict=False)
    )


@pytest.mark.asyncio
async def test_short_transcript_uses_two_call_fast_path(tmp_path):
    request = _request(
        tmp_path,
        LongFormConfig(threshold_tokens=10_000),
        segment_count=2,
    )
    llm = RecordingLlm()

    submission = await ApiEnrichmentExecutor(llm).submit(request)

    assert len(llm.calls) == 2
    trace = json.loads(
        submission.artifacts[KIND_ENRICHMENT_PLAN_JSON].read_text(encoding="utf-8")
    )
    assert trace["strategy"] == "single_pass"
    assert trace["hierarchy"] == []


@pytest.mark.asyncio
async def test_long_transcript_recursively_reduces_and_records_provenance(tmp_path):
    request = _request(
        tmp_path,
        LongFormConfig(
            threshold_tokens=1,
            chunk_token_budget=45,
            chunk_duration_s=120,
            reduce_token_budget=100,
            reduce_group_size=2,
        ),
    )
    llm = RecordingLlm()

    submission = await ApiEnrichmentExecutor(llm).submit(request)
    trace_path = submission.artifacts[KIND_ENRICHMENT_PLAN_JSON]
    trace = json.loads(trace_path.read_text(encoding="utf-8"))

    assert trace["strategy"] == "hierarchical"
    assert len(trace["chunks"]) == 8
    assert max(node["level"] for node in trace["hierarchy"]) >= 3
    assert trace["cache_misses"] > len(trace["chunks"])
    assert "content" not in trace_path.read_text(encoding="utf-8")
    assert "enrichment_pipeline: 1.0" in submission.artifacts[
        "abstract_md"
    ].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_intermediate_cache_reuses_chunks_and_reductions(tmp_path):
    request = _request(
        tmp_path,
        LongFormConfig(
            threshold_tokens=1,
            chunk_token_budget=45,
            reduce_group_size=2,
        ),
    )
    first = RecordingLlm()
    second = RecordingLlm()
    pipeline = LongFormEnrichmentPipeline(
        request.long_form,
        request.cache_root,
    )

    initial = await pipeline.run(request, first)
    repeated = await pipeline.run(request, second)

    assert len(first.calls) > 0
    assert len(second.calls) == 0
    assert initial.trace.cache_misses > 0
    assert repeated.trace.cache_hits == initial.trace.cache_misses
    assert repeated.context == initial.context


@pytest.mark.asyncio
async def test_failed_chunk_retry_reuses_successful_siblings(tmp_path):
    request = _request(
        tmp_path,
        LongFormConfig(
            threshold_tokens=1,
            chunk_token_budget=45,
            reduce_group_size=2,
        ),
        segment_count=4,
    )
    pipeline = LongFormEnrichmentPipeline(request.long_form, request.cache_root)
    failing = RecordingLlm(fail_at=3)

    with pytest.raises(RuntimeError, match="fixture chunk failure"):
        await pipeline.run(request, failing)

    retry = RecordingLlm()
    result = await pipeline.run(request, retry)

    assert result.trace.cache_hits == 2
    assert len(retry.calls) < 4 + 3


def test_external_agent_manifest_uses_the_same_deterministic_plan(tmp_path):
    request = _request(
        tmp_path,
        LongFormConfig(
            threshold_tokens=1,
            chunk_token_budget=45,
            reduce_group_size=2,
        ),
    )
    expected = TranscriptChunkPlanner(request.long_form).plan(request.normalized)

    manifest = external_manifest(request)

    assert manifest["pipeline"]["plan_hash"] == expected.plan_hash
    assert [item["id"] for item in manifest["pipeline"]["chunk_operations"]] == [
        chunk.id for chunk in expected.chunks
    ]
