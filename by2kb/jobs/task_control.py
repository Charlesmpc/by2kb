from __future__ import annotations

import time
from pathlib import Path

from by2kb.config import Config
from by2kb.errors import ConfigError
from by2kb.jobs.model import Job, JobStatus, TERMINAL_STATES
from by2kb.jobs.runner import ingest_source
from by2kb.jobs.store import JobStore

TASK_PROTOCOL_SCHEMA_VERSION = 1
MAX_WAIT_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 0.25

RETRYABLE_STATES = frozenset(
    {
        JobStatus.FAILED_RETRYABLE,
        JobStatus.RATE_LIMITED,
        JobStatus.NEEDS_AUTH,
    }
)

_STAGE = {
    JobStatus.ACCEPTED: "queued",
    JobStatus.RESOLVING: "resolving",
    JobStatus.FETCHING_TRANSCRIPT: "transcript",
    JobStatus.CAPTURING_MEDIA: "media",
    JobStatus.TRANSCRIBING: "asr",
    JobStatus.NORMALIZING: "normalizing",
    JobStatus.RAW_PUBLISHED: "raw_published",
    JobStatus.ENRICHMENT_PENDING: "enrichment_pending",
    JobStatus.ENRICHING: "enrichment",
    JobStatus.UPDATED_PUBLISHED: "publishing",
    JobStatus.COMPLETED: "completed",
    JobStatus.NEEDS_AUTH: "blocked",
    JobStatus.NO_NATIVE_TRANSCRIPT: "failed",
    JobStatus.NEEDS_AUDIO_FALLBACK: "media",
    JobStatus.RATE_LIMITED: "retryable_failure",
    JobStatus.FAILED_RETRYABLE: "retryable_failure",
    JobStatus.FAILED_TERMINAL: "failed",
    JobStatus.CANCELLED: "cancelled",
}

_PROGRESS = {
    JobStatus.ACCEPTED: 0.0,
    JobStatus.RESOLVING: 0.1,
    JobStatus.FETCHING_TRANSCRIPT: 0.2,
    JobStatus.CAPTURING_MEDIA: 0.25,
    JobStatus.TRANSCRIBING: 0.45,
    JobStatus.NORMALIZING: 0.65,
    JobStatus.RAW_PUBLISHED: 0.75,
    JobStatus.ENRICHMENT_PENDING: 0.8,
    JobStatus.ENRICHING: 0.85,
    JobStatus.UPDATED_PUBLISHED: 0.95,
    JobStatus.COMPLETED: 1.0,
    JobStatus.NEEDS_AUTH: 0.0,
    JobStatus.NO_NATIVE_TRANSCRIPT: 0.0,
    JobStatus.NEEDS_AUDIO_FALLBACK: 0.2,
    JobStatus.RATE_LIMITED: 0.0,
    JobStatus.FAILED_RETRYABLE: 0.0,
    JobStatus.FAILED_TERMINAL: 0.0,
    JobStatus.CANCELLED: 0.0,
}


def task_status(config: Config, job_id: str) -> dict[str, object]:
    store = JobStore(config.db_path)
    try:
        job = _required_job(store, job_id)
        return _snapshot(store, job, event="snapshot")
    finally:
        store.close()


def wait_for_task(
    config: Config,
    job_id: str,
    *,
    timeout_s: float,
) -> dict[str, object]:
    if timeout_s < 0 or timeout_s > MAX_WAIT_TIMEOUT_S:
        raise ConfigError(
            f"wait timeout must be between 0 and {MAX_WAIT_TIMEOUT_S:.0f} seconds"
        )
    store = JobStore(config.db_path)
    try:
        initial = _required_job(store, job_id)
        initial_token = _change_token(store, initial)
        if initial.status in TERMINAL_STATES:
            return _snapshot(store, initial, event="terminal")
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                current = _required_job(store, job_id)
                return _snapshot(store, current, event="timeout")
            time.sleep(min(POLL_INTERVAL_S, remaining))
            current = _required_job(store, job_id)
            if current.status in TERMINAL_STATES:
                return _snapshot(store, current, event="terminal")
            if _change_token(store, current) != initial_token:
                return _snapshot(store, current, event="state_changed")
    finally:
        store.close()


def cancel_task(config: Config, job_id: str) -> dict[str, object]:
    store = JobStore(config.db_path)
    try:
        job = _required_job(store, job_id)
        if job.status == JobStatus.CANCELLED:
            return _snapshot(store, job, event="already_cancelled")
        if job.status in TERMINAL_STATES:
            raise ConfigError(f"cannot cancel job in terminal state {job.status.value}")
        store.request_cancel(job_id)
        if job.status in {
            JobStatus.ACCEPTED,
            JobStatus.ENRICHMENT_PENDING,
            JobStatus.NEEDS_AUDIO_FALLBACK,
        }:
            store.update_status(
                job_id,
                JobStatus.CANCELLED,
                error_category="JobCancelled",
                error_message="cancelled before the next active stage",
            )
            current = _required_job(store, job_id)
            return _snapshot(store, current, event="cancelled")
        current = _required_job(store, job_id)
        return _snapshot(store, current, event="cancellation_requested")
    finally:
        store.close()


async def retry_task(config: Config, job_id: str) -> dict[str, object]:
    store = JobStore(config.db_path)
    try:
        job = _required_job(store, job_id)
        if job.status not in RETRYABLE_STATES:
            allowed = ", ".join(sorted(status.value for status in RETRYABLE_STATES))
            raise ConfigError(
                f"job state {job.status.value} is not retryable; allowed: {allowed}"
            )
        source = job.options.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ConfigError(
                "job predates retry source tracking; run by2kb ingest again explicitly"
            )
        task = store.get_enrichment_task(job_id)
        artifacts = {item["kind"]: item["path"] for item in store.artifacts(job_id)}
        can_reuse_transcript = bool(task) and all(
            kind in artifacts and Path(artifacts[kind]).is_file()
            for kind in ("transcript_json", "raw_md")
        )
        store.clear_cancel(job_id)
        store.update_status(job_id, JobStatus.ACCEPTED)
        executor = task["executor"] if task else None
    finally:
        store.close()

    outcome = await ingest_source(
        source,
        config,
        re_enrich=can_reuse_transcript,
        refresh=not can_reuse_transcript,
        enricher=executor,
    )
    payload = task_status(config, job_id)
    payload["event"] = "retried"
    payload["retry_outcome"] = outcome.to_dict()
    payload["reused_transcript"] = can_reuse_transcript
    return payload


def _required_job(store: JobStore, job_id: str) -> Job:
    job = store.get_job(job_id)
    if job is None:
        raise ConfigError(f"job not found: {job_id}")
    return job


def _snapshot(
    store: JobStore,
    job: Job,
    *,
    event: str,
) -> dict[str, object]:
    terminal = job.status in TERMINAL_STATES
    retryable = job.status in RETRYABLE_STATES
    message = job.error_message or _default_message(job.status)
    return {
        "schema_version": TASK_PROTOCOL_SCHEMA_VERSION,
        "event": event,
        "job_id": job.id,
        "state": job.status.value,
        "stage": _STAGE[job.status],
        "progress": _PROGRESS[job.status],
        "message": message,
        "terminal": terminal,
        "retryable": retryable,
        "cancel_requested": job.cancel_requested,
        "error": (
            {
                "category": job.last_error_category,
                "message": job.error_message,
            }
            if job.last_error_category or job.error_message
            else None
        ),
        "artifacts": store.artifacts(job.id),
        "updated_at": job.updated_at,
    }


def _change_token(store: JobStore, job: Job) -> tuple:
    artifacts = store.artifacts(job.id)
    return (
        job.status.value,
        job.updated_at,
        job.cancel_requested,
        tuple((item["kind"], item["content_hash"]) for item in artifacts),
    )


def _default_message(status: JobStatus) -> str:
    messages = {
        JobStatus.ENRICHMENT_PENDING: "transcript ready; waiting for Agent enrichment",
        JobStatus.COMPLETED: "all artifacts published",
        JobStatus.CANCELLED: "job cancelled",
    }
    return messages.get(status, status.value.replace("_", " "))
