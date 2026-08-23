from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from by2kb.config import Config
from by2kb.enrichment import (
    KIND_ABSTRACT_MD,
    KIND_UPDATED_MD,
    create_enrichment_request,
    external_manifest,
    write_external_artifacts,
)
from by2kb.errors import ConfigError
from by2kb.jobs.model import JobStatus
from by2kb.jobs.store import JobStore
from by2kb.normalize import NormalizedTranscript
from by2kb.sinks.filesystem import FilesystemSink
from by2kb.writers.raw import content_hash

KIND_TRANSCRIPT_JSON = "transcript_json"
KIND_RAW_MD = "raw_md"


@dataclass(frozen=True)
class EnrichmentResult:
    job_id: str
    status: str
    artifacts: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "artifacts": self.artifacts,
        }


def claim_external_enrichment(config: Config, job_id: str) -> dict:
    store = JobStore(config.db_path)
    try:
        job, task, request = _load_external_request(store, config, job_id)
        if task["status"] == "completed":
            raise ConfigError(f"enrichment already completed: {job_id}")
        if task["status"] not in {"pending", "claimed", "failed_retryable"}:
            raise ConfigError(
                f"enrichment task cannot be claimed from status {task['status']}"
            )
        store.update_enrichment_task(job_id, "claimed")
        if job.status != JobStatus.ENRICHING:
            store.update_status(job_id, JobStatus.ENRICHING)
        manifest = external_manifest(request)
        manifest["status"] = "claimed"
        return manifest
    finally:
        store.close()


async def complete_external_enrichment(
    config: Config,
    job_id: str,
    *,
    abstract_path: Path,
    study_path: Path,
    provider: str,
    model: str,
) -> EnrichmentResult:
    store = JobStore(config.db_path)
    try:
        job, task, request = _load_external_request(store, config, job_id)
        if task["status"] not in {"pending", "claimed", "failed_retryable"}:
            raise ConfigError(
                f"enrichment task cannot be completed from status {task['status']}"
            )
        for label, path in (("abstract", abstract_path), ("study", study_path)):
            if not path.is_file():
                raise ConfigError(f"{label} output not found: {path}")
        generated = write_external_artifacts(
            request,
            abstract_body=abstract_path.read_text(encoding="utf-8"),
            study_body=study_path.read_text(encoding="utf-8"),
            provider=provider or "external_agent",
            model=model or "host-model",
        )
        receipt = await FilesystemSink(config.library_root).publish(
            generated,
            platform=job.platform,
            video_id=job.video_id,
        )
        for kind in (KIND_ABSTRACT_MD, KIND_UPDATED_MD):
            store.add_artifact(
                job_id,
                kind,
                receipt.artifacts[kind],
                content_hash(generated[kind]),
            )
        store.update_enrichment_task(
            job_id,
            "completed",
            provider=provider or "external_agent",
            model=model or "host-model",
        )
        store.update_status(job_id, JobStatus.UPDATED_PUBLISHED)
        store.update_status(job_id, JobStatus.COMPLETED)
        return EnrichmentResult(
            job_id=job_id,
            status=JobStatus.COMPLETED.value,
            artifacts=_artifact_map(store, job_id),
        )
    except Exception as exc:
        task = store.get_enrichment_task(job_id)
        if task and task["status"] != "completed":
            store.update_enrichment_task(
                job_id,
                "failed_retryable",
                error_message=str(exc),
            )
        raise
    finally:
        store.close()


def fail_external_enrichment(
    config: Config,
    job_id: str,
    *,
    message: str,
    retryable: bool = True,
) -> EnrichmentResult:
    store = JobStore(config.db_path)
    try:
        job = store.get_job(job_id)
        task = store.get_enrichment_task(job_id)
        if job is None or task is None:
            raise ConfigError(f"external enrichment task not found: {job_id}")
        if task["executor"] != "external_agent":
            raise ConfigError(f"job does not use external agent enrichment: {job_id}")
        task_status = "failed_retryable" if retryable else "failed_terminal"
        job_status = (
            JobStatus.FAILED_RETRYABLE if retryable else JobStatus.FAILED_TERMINAL
        )
        store.update_enrichment_task(job_id, task_status, error_message=message)
        store.update_status(
            job_id,
            job_status,
            error_category="ExternalEnrichmentError",
            error_message=message,
        )
        return EnrichmentResult(
            job_id=job_id,
            status=job_status.value,
            artifacts=_artifact_map(store, job_id),
        )
    finally:
        store.close()


def _load_external_request(store: JobStore, config: Config, job_id: str):
    job = store.get_job(job_id)
    task = store.get_enrichment_task(job_id)
    if job is None or task is None:
        raise ConfigError(f"external enrichment task not found: {job_id}")
    if task["executor"] != "external_agent":
        raise ConfigError(f"job does not use external agent enrichment: {job_id}")
    artifacts = _artifact_map(store, job_id)
    missing = [
        kind
        for kind in (KIND_TRANSCRIPT_JSON, KIND_RAW_MD)
        if kind not in artifacts or not Path(artifacts[kind]).is_file()
    ]
    if missing:
        raise ConfigError("missing enrichment inputs: " + ", ".join(missing))
    normalized = NormalizedTranscript.model_validate_json(
        Path(artifacts[KIND_TRANSCRIPT_JSON]).read_text(encoding="utf-8")
    )
    request = create_enrichment_request(
        config,
        job_id=job_id,
        normalized=normalized,
        raw_path=Path(artifacts[KIND_RAW_MD]),
        staging=config.home / "jobs" / job_id / "artifacts",
        abstract_profile=task["abstract_profile"],
        study_profile=task["study_profile"],
    )
    return job, task, request


def _artifact_map(store: JobStore, job_id: str) -> dict[str, str]:
    return {item["kind"]: item["path"] for item in store.artifacts(job_id)}
