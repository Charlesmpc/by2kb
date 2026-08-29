from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from by2kb.config import Config, LongFormConfig
from by2kb.errors import ConfigError
from by2kb.integrations.hermes import (
    _run_staged_enrichment,
)
from by2kb.jobs.enrichment_service import (
    next_external_enrichment_operation,
    submit_external_enrichment_operation,
)
from by2kb.jobs.model import Job, JobStatus
from by2kb.jobs.store import JobStore
from by2kb.normalize import from_asr_result
from by2kb.providers.asr import AsrResult
from by2kb.providers.base import SourceIdentity
from by2kb.writers.raw import content_hash, write_artifacts


def _external_job(tmp_path: Path, *, long: bool = True) -> tuple[Config, str]:
    config = Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "home" / "jobs.db",
        enrichment_executor="external_agent",
        long_form=LongFormConfig(
            threshold_tokens=1 if long else 10_000,
            chunk_token_budget=45,
            reduce_group_size=2,
        ),
    )
    identity = SourceIdentity(
        platform="test",
        video_id="agent-provider",
        canonical_url="test://agent-provider",
    )
    segments = []
    for index in range(4):
        text = "".join(chr(0x4E00 + index * 40 + offset) for offset in range(40))
        segments.append(
            {
                "start": index * 60,
                "end": (index + 1) * 60,
                "text": text,
            }
        )
    normalized = from_asr_result(
        identity,
        title="Agent provider fixture",
        author="by2kb",
        duration_ms=240_000,
        asr_result=AsrResult(
            provider="fixture",
            model="fixture",
            text="".join(segment["text"] for segment in segments),
            segments=segments,
        ),
        fetched_at="2026-08-28T00:00:00Z",
    )
    artifacts = write_artifacts(
        tmp_path / "inputs",
        source_payload={"fixture": True},
        normalized=normalized,
    )
    job_id = "agent-provider-job"
    store = JobStore(config.db_path)
    try:
        store.create_job(
            Job(
                id=job_id,
                platform=identity.platform,
                video_id=identity.video_id,
                status=JobStatus.ENRICHMENT_PENDING,
            )
        )
        for kind, path in artifacts.items():
            store.add_artifact(job_id, kind, str(path), content_hash(path))
        store.upsert_enrichment_task(
            job_id,
            status="pending",
            executor="external_agent",
            abstract_profile="short-video-abstract",
            study_profile="default-video-digest",
        )
    finally:
        store.close()
    return config, job_id


@pytest.mark.asyncio
async def test_staged_agent_provider_completes_shared_long_form_plan(tmp_path):
    config, job_id = _external_job(tmp_path)
    operations = []

    for index in range(32):
        step = await next_external_enrichment_operation(
            config,
            job_id,
            provider="hermes",
            model="subscription-profile",
            runtime_version="test-v1",
        )
        if step["status"] == "completed":
            break
        assert step["status"] == "needs_input"
        operation = step["operation"]
        operations.append(operation)
        output = tmp_path / f"operation-{index}.md"
        output.write_text(f"# Grounded Agent result {index}", encoding="utf-8")
        accepted = submit_external_enrichment_operation(
            config,
            job_id,
            operation_id=operation["id"],
            output_path=output,
            provider="hermes",
            model="subscription-profile",
            runtime_version="test-v1",
        )
        assert accepted["status"] == "accepted"
    else:  # pragma: no cover - loop safety
        pytest.fail("staged Agent pipeline did not finish")

    assert len(operations) > 2
    assert set(step["artifacts"]) >= {
        "raw_md",
        "abstract_md",
        "updated_md",
        "enrichment_plan_json",
    }
    rendered = Path(step["artifacts"]["updated_md"]).read_text(encoding="utf-8")
    assert "provider: hermes" in rendered
    assert "model: subscription-profile" in rendered


@pytest.mark.asyncio
async def test_agent_session_identity_prevents_cross_runtime_submission(tmp_path):
    config, job_id = _external_job(tmp_path, long=False)
    step = await next_external_enrichment_operation(
        config,
        job_id,
        provider="hermes",
        model="profile-a",
    )
    output = tmp_path / "output.md"
    output.write_text("# Result", encoding="utf-8")

    with pytest.raises(ConfigError, match="no Agent enrichment operation"):
        submit_external_enrichment_operation(
            config,
            job_id,
            operation_id=step["operation"]["id"],
            output_path=output,
            provider="hermes",
            model="profile-b",
        )


def test_hermes_adapter_uses_bounded_next_submit_contract(tmp_path, monkeypatch):
    calls = []
    operation = {
        "id": "operation-1",
        "system_prompt": "system",
        "user_prompt": "short-video-abstract",
        "max_output_bytes": 1024,
        "timeout_s": 10,
    }

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1] == "next" and len(
            [call for call in calls if call[1] == "next"]
        ) == 1:
            return {"status": "needs_input", "operation": operation}
        if arguments[1] == "submit":
            return {"status": "accepted"}
        return {
            "status": "completed",
            "artifacts": {"abstract_md": str(tmp_path / "short.md")},
        }

    class HostLlm:
        provider = "hermes"
        model = "subscription-profile"
        runtime_version = "test-v1"

        def complete(self, **_kwargs):
            return SimpleNamespace(text="# Host result")

    monkeypatch.setattr("by2kb.integrations.hermes._run_by2kb", run)
    result = _run_staged_enrichment(SimpleNamespace(llm=HostLlm()), "job-1")

    assert result["status"] == "completed"
    assert [call[1] for call in calls] == ["next", "submit", "next"]
    assert all(call[1] != "claim" for call in calls)
