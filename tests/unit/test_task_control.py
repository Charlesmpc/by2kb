from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from by2kb import cli
from by2kb.config import Config
from by2kb.errors import ConfigError, JobCancelled
from by2kb.jobs.model import Job, JobStatus
from by2kb.jobs.runner import IngestOutcome, _raise_if_cancelled
from by2kb.jobs.store import JobStore
from by2kb.jobs.task_control import (
    cancel_task,
    retry_task,
    task_status,
    wait_for_task,
)


def _config(tmp_path: Path) -> Config:
    return Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "home" / "jobs.db",
        enrichment_executor="external_agent",
    )


def _create_job(
    config: Config,
    *,
    job_id: str = "task-job",
    status: JobStatus = JobStatus.ACCEPTED,
    source: str = "https://www.bilibili.com/video/BV1taskcontrol/",
) -> Job:
    job = Job(
        id=job_id,
        platform="bilibili",
        video_id="BV1taskcontrol",
        status=status,
        options={"source": source, "source_kind": "url"},
    )
    store = JobStore(config.db_path)
    try:
        store.create_job(job)
    finally:
        store.close()
    return job


def test_status_returns_versioned_agent_envelope(tmp_path):
    config = _config(tmp_path)
    _create_job(config, status=JobStatus.TRANSCRIBING)

    payload = task_status(config, "task-job")

    assert payload["schema_version"] == 1
    assert payload["state"] == "transcribing"
    assert payload["stage"] == "asr"
    assert payload["progress"] == 0.45
    assert payload["terminal"] is False
    assert payload["retryable"] is False
    assert payload["artifacts"] == []


def test_store_migrates_existing_jobs_database_for_cancellation(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """CREATE TABLE jobs (
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, video_id TEXT NOT NULL,
                status TEXT NOT NULL, requested_by TEXT, destination TEXT,
                options_json TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error_category TEXT, error_message TEXT,
                created_at TEXT NOT NULL, updated_at TEXT
            )"""
        )
        connection.commit()
    finally:
        connection.close()

    store = JobStore(db_path)
    try:
        store.create_job(
            Job(id="migrated", platform="local", video_id="legacy")
        )
        store.request_cancel("migrated")
        migrated = store.get_job("migrated")
    finally:
        store.close()

    assert migrated is not None
    assert migrated.cancel_requested is True


def test_wait_timeout_does_not_fail_job(tmp_path):
    config = _config(tmp_path)
    _create_job(config, status=JobStatus.ENRICHING)

    payload = wait_for_task(config, "task-job", timeout_s=0)

    assert payload["event"] == "timeout"
    assert payload["state"] == "enriching"
    assert payload["terminal"] is False
    assert task_status(config, "task-job")["state"] == "enriching"


def test_wait_returns_on_state_change(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _create_job(config, status=JobStatus.TRANSCRIBING)
    changed = False

    def advance(_duration):
        nonlocal changed
        if changed:
            return
        changed = True
        store = JobStore(config.db_path)
        try:
            store.update_status("task-job", JobStatus.NORMALIZING)
        finally:
            store.close()

    monkeypatch.setattr("by2kb.jobs.task_control.time.sleep", advance)
    payload = wait_for_task(config, "task-job", timeout_s=1)

    assert payload["event"] == "state_changed"
    assert payload["state"] == "normalizing"


def test_cancel_pending_is_terminal_and_idempotent(tmp_path):
    config = _config(tmp_path)
    _create_job(config, status=JobStatus.ENRICHMENT_PENDING)

    first = cancel_task(config, "task-job")
    second = cancel_task(config, "task-job")

    assert first["event"] == "cancelled"
    assert first["state"] == "cancelled"
    assert second["event"] == "already_cancelled"
    assert second["state"] == "cancelled"


def test_active_cancel_is_honored_at_pipeline_boundary(tmp_path):
    config = _config(tmp_path)
    job = _create_job(config, status=JobStatus.TRANSCRIBING)

    requested = cancel_task(config, "task-job")
    assert requested["event"] == "cancellation_requested"

    store = JobStore(config.db_path)
    try:
        with pytest.raises(JobCancelled):
            _raise_if_cancelled(store, job)
        assert store.get_job(job.id).status == JobStatus.CANCELLED
    finally:
        store.close()


@pytest.mark.asyncio
async def test_retry_reuses_transcript_for_failed_agent_enrichment(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    job = _create_job(config, status=JobStatus.FAILED_RETRYABLE)
    raw = tmp_path / "raw.md"
    transcript = tmp_path / "transcript.json"
    raw.write_text("# raw", encoding="utf-8")
    transcript.write_text("{}", encoding="utf-8")
    store = JobStore(config.db_path)
    try:
        store.add_artifact(job.id, "raw_md", str(raw), "raw-hash")
        store.add_artifact(
            job.id,
            "transcript_json",
            str(transcript),
            "transcript-hash",
        )
        store.upsert_enrichment_task(
            job.id,
            status="failed_retryable",
            executor="external_agent",
            abstract_profile="short-video-abstract",
            study_profile="default-video-digest",
        )
    finally:
        store.close()
    calls = []

    async def ingest(source, _config, **options):
        calls.append((source, options))
        update = JobStore(config.db_path)
        try:
            update.update_status(job.id, JobStatus.ENRICHMENT_PENDING)
        finally:
            update.close()
        return IngestOutcome(
            exit_code=0,
            job_id=job.id,
            status="enrichment_pending",
        )

    monkeypatch.setattr("by2kb.jobs.task_control.ingest_source", ingest)
    payload = await retry_task(config, job.id)

    assert calls[0][1]["re_enrich"] is True
    assert calls[0][1]["refresh"] is False
    assert calls[0][1]["enricher"] == "external_agent"
    assert payload["event"] == "retried"
    assert payload["reused_transcript"] is True


@pytest.mark.asyncio
async def test_retry_rejects_nonrecoverable_state(tmp_path):
    config = _config(tmp_path)
    _create_job(config, status=JobStatus.FAILED_TERMINAL)

    with pytest.raises(ConfigError, match="is not retryable"):
        await retry_task(config, "task-job")


def test_status_cli_emits_versioned_json(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _create_job(config, status=JobStatus.ENRICHMENT_PENDING)
    monkeypatch.setattr(cli, "load_config", lambda: config)

    result = CliRunner().invoke(cli.app, ["status", "task-job", "--json"])

    assert result.exit_code == 0
    assert '"schema_version": 1' in result.stdout
    assert '"state": "enrichment_pending"' in result.stdout
