from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from by2kb.agent_runtime import (
    AgentCallbackClient,
    AgentOperationRequired,
    AgentSessionStore,
    MAX_AGENT_OUTPUT_BYTES,
)
from by2kb.config import Config
from by2kb.enrichment import (
    KIND_ABSTRACT_MD,
    KIND_UPDATED_MD,
    LlmEnrichmentProvider,
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
AGENT_PROTOCOL_SCHEMA_VERSION = 1


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


async def next_external_enrichment_operation(
    config: Config,
    job_id: str,
    *,
    provider: str,
    model: str,
    runtime_version: str = "",
) -> dict[str, object]:
    """Advance a staged Agent enrichment session until host output is required."""
    store = JobStore(config.db_path)
    try:
        job, task, request = _load_external_request(store, config, job_id)
        if task["status"] == "completed":
            return _agent_envelope(
                job_id,
                status="completed",
                artifacts=_artifact_map(store, job_id),
            )
        if task["status"] not in {"pending", "claimed", "failed_retryable"}:
            raise ConfigError(
                f"Agent enrichment cannot advance from status {task['status']}"
            )
        session = AgentSessionStore(
            config.home / "jobs" / job_id / "agent-enrichment",
            provider=provider,
            model=model,
            runtime_version=runtime_version,
        )
        client = AgentCallbackClient(session)
        try:
            submission = await LlmEnrichmentProvider(client).submit(request)
        except AgentOperationRequired as required:
            store.update_enrichment_task(
                job_id,
                "claimed",
                provider=client.provider,
                model=client.model,
            )
            if job.status != JobStatus.ENRICHING:
                store.update_status(job_id, JobStatus.ENRICHING)
            return _agent_envelope(
                job_id,
                status="needs_input",
                operation=required.operation.to_dict(),
                artifacts=_artifact_map(store, job_id),
            )

        receipt = await FilesystemSink(config.library_root).publish(
            submission.artifacts,
            platform=job.platform,
            video_id=job.video_id,
        )
        for kind, generated in submission.artifacts.items():
            store.add_artifact(
                job_id,
                kind,
                receipt.artifacts[kind],
                content_hash(generated),
            )
        store.update_enrichment_task(
            job_id,
            "completed",
            provider=client.provider,
            model=client.model,
        )
        store.update_status(job_id, JobStatus.UPDATED_PUBLISHED)
        store.update_status(job_id, JobStatus.COMPLETED)
        return _agent_envelope(
            job_id,
            status="completed",
            artifacts=_artifact_map(store, job_id),
        )
    finally:
        store.close()


def submit_external_enrichment_operation(
    config: Config,
    job_id: str,
    *,
    operation_id: str,
    output_path: Path,
    provider: str,
    model: str,
    runtime_version: str = "",
) -> dict[str, object]:
    store = JobStore(config.db_path)
    try:
        job, task, _request = _load_external_request(store, config, job_id)
        if task["status"] not in {"pending", "claimed", "failed_retryable"}:
            raise ConfigError(
                f"Agent enrichment cannot accept output from status {task['status']}"
            )
        if not output_path.is_file():
            raise ConfigError(f"Agent operation output not found: {output_path}")
        if output_path.stat().st_size > MAX_AGENT_OUTPUT_BYTES:
            raise ConfigError(
                f"Agent enrichment output exceeds {MAX_AGENT_OUTPUT_BYTES} bytes"
            )
        try:
            content = output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigError("Agent operation output must be readable UTF-8") from exc
        session = AgentSessionStore(
            config.home / "jobs" / job_id / "agent-enrichment",
            provider=provider,
            model=model,
            runtime_version=runtime_version,
        )
        session.submit(operation_id, content)
        store.update_enrichment_task(
            job_id,
            "claimed",
            provider=provider,
            model=model,
        )
        if job.status != JobStatus.ENRICHING:
            store.update_status(job_id, JobStatus.ENRICHING)
        return _agent_envelope(
            job_id,
            status="accepted",
            artifacts=_artifact_map(store, job_id),
        )
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


def _agent_envelope(
    job_id: str,
    *,
    status: str,
    operation: dict[str, object] | None = None,
    artifacts: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": AGENT_PROTOCOL_SCHEMA_VERSION,
        "job_id": job_id,
        "status": status,
        "operation": operation,
        "artifacts": artifacts or {},
    }
