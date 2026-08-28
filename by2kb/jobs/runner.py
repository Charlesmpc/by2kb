from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from by2kb.config import Config
from by2kb.enrichment import (
    ApiEnrichmentExecutor,
    DisabledEnrichmentExecutor,
    ExternalAgentEnrichmentExecutor,
    create_enrichment_request,
)
from by2kb.errors import (
    By2kbError,
    ConfigError,
    DuplicateJob,
    UnsupportedUrl,
    category_of,
)
from by2kb.jobs.model import STATUS_FOR_ERROR, Job, JobStatus
from by2kb.jobs.store import JobStore
from by2kb.normalize import NormalizedTranscript, from_asr_result
from by2kb.providers import bilibili
from by2kb.providers.asr import AsrOptions
from by2kb.providers.asr_registry import AsrProviderRegistry, build_default_asr_registry
from by2kb.providers.base import FetchOptions, LocalAudio, SourceIdentity
from by2kb.providers.bilibili_wbi import WbiKeyCache
from by2kb.providers.local_media import (
    LocalMediaInfo,
    inspect_local_media,
    prepare_local_audio,
)
from by2kb.sinks.filesystem import FilesystemSink
from by2kb.skills.runner import LlmClient, OpenAiCompatibleClient
from by2kb.writers.raw import content_hash, write_artifacts

EXIT_COMPLETED = 0
EXIT_DUPLICATE = 4

KIND_SOURCE_JSON = "source_json"
KIND_TRANSCRIPT_JSON = "transcript_json"
KIND_RAW_MD = "raw_md"


@dataclass
class IngestOutcome:
    exit_code: int
    job_id: str | None = None
    status: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "job_id": self.job_id,
            "status": self.status,
            "artifacts": self.artifacts,
            "message": self.message,
        }


@dataclass(frozen=True)
class PreparedMedia:
    title: str
    author: str
    duration_s: float | None
    audio: LocalAudio
    source_payload: dict[str, object]


ResolveSource = Callable[[httpx.AsyncClient], Awaitable[SourceIdentity]]
PrepareMedia = Callable[
    [httpx.AsyncClient, SourceIdentity, Path, FetchOptions, JobStore, Job],
    Awaitable[PreparedMedia],
]


async def build_summary_artifacts(
    config: Config,
    normalized: NormalizedTranscript,
    raw_path: Path,
    staging: Path,
    llm: LlmClient,
) -> dict[str, Path]:
    """Compatibility wrapper for callers using the original API helper."""
    request = create_enrichment_request(
        config,
        job_id="standalone",
        normalized=normalized,
        raw_path=raw_path,
        staging=staging,
    )
    submission = await ApiEnrichmentExecutor(llm).submit(request)
    return submission.artifacts


async def resolve_url(url: str, client: httpx.AsyncClient):
    candidate = (url or "").strip()
    lowered = candidate.lower()
    if "b23.tv" in lowered:
        target = await bilibili.expand_short_url(client, candidate)
        return bilibili.resolve(target)
    if "bilibili.com" in lowered or candidate.startswith("BV"):
        return bilibili.resolve(candidate)
    if "youtube.com" in lowered or "youtu.be" in lowered:
        raise UnsupportedUrl(
            "YouTube support is not implemented yet (Milestone 1 is Bilibili-first)"
        )
    raise UnsupportedUrl(f"unsupported URL: {url}")


def _failure(store: JobStore, job: Job | None, error: By2kbError) -> IngestOutcome:
    status = STATUS_FOR_ERROR.get(category_of(error), JobStatus.FAILED_TERMINAL)
    if job is not None:
        store.update_status(
            job.id,
            status,
            error_category=category_of(error),
            error_message=str(error),
        )
    return IngestOutcome(
        exit_code=error.exit_code,
        job_id=job.id if job else None,
        status=status.value,
        message=f"{category_of(error)}: {error}",
    )


def _stored_artifacts(store: JobStore, job_id: str) -> dict[str, str]:
    return {item["kind"]: item["path"] for item in store.artifacts(job_id)}


async def _run_enrichment(
    *,
    store: JobStore,
    job: Job,
    config: Config,
    normalized: NormalizedTranscript,
    raw_path: Path,
    staging: Path,
    client: httpx.AsyncClient,
    executor_name: str,
) -> IngestOutcome:
    request = create_enrichment_request(
        config,
        job_id=job.id,
        normalized=normalized,
        raw_path=raw_path,
        staging=staging,
    )
    abstract_profile = request.abstract_skill.name
    study_profile = request.study_skill.name

    if executor_name == "disabled":
        executor = DisabledEnrichmentExecutor()
    elif executor_name == "external_agent":
        executor = ExternalAgentEnrichmentExecutor()
    elif executor_name == "api":
        if not config.llm.usable:
            raise ConfigError(
                "api enrichment requires BY2KB_LLM_API_KEY and BY2KB_LLM_MODEL"
            )
        executor = ApiEnrichmentExecutor(OpenAiCompatibleClient(config.llm, client))
    else:
        raise ConfigError(f"unknown enrichment executor: {executor_name}")

    if executor_name != "disabled":
        store.upsert_enrichment_task(
            job.id,
            status="pending" if executor_name == "external_agent" else "enriching",
            executor=executor_name,
            abstract_profile=abstract_profile,
            study_profile=study_profile,
            provider=("openai_compatible" if executor_name == "api" else None),
            model=(config.llm.model if executor_name == "api" else None),
        )

    submission = await executor.submit(request)
    if submission.deferred:
        store.update_status(job.id, JobStatus.ENRICHMENT_PENDING)
        artifacts = _stored_artifacts(store, job.id)
        return IngestOutcome(
            exit_code=EXIT_COMPLETED,
            job_id=job.id,
            status=JobStatus.ENRICHMENT_PENDING.value,
            artifacts=artifacts,
            message=f"transcribed; awaiting agent enrichment: {job.id}",
        )

    if submission.artifacts:
        store.update_status(job.id, JobStatus.ENRICHING)
        receipt = await FilesystemSink(config.library_root).publish(
            submission.artifacts,
            platform=job.platform,
            video_id=job.video_id,
        )
        for kind, path in submission.artifacts.items():
            store.add_artifact(job.id, kind, receipt.artifacts[kind], content_hash(path))
        store.update_enrichment_task(
            job.id,
            "completed",
            provider="openai_compatible",
            model=config.llm.model,
        )
        store.update_status(job.id, JobStatus.UPDATED_PUBLISHED)

    store.update_status(job.id, JobStatus.COMPLETED)
    artifacts = _stored_artifacts(store, job.id)
    return IngestOutcome(
        exit_code=EXIT_COMPLETED,
        job_id=job.id,
        status=JobStatus.COMPLETED.value,
        artifacts=artifacts,
        message=f"completed: {config.library_root / job.platform / job.video_id}",
    )


async def ingest_source(
    source: str,
    config: Config,
    *,
    refresh: bool = False,
    re_enrich: bool = False,
    enricher: str | None = None,
    requested_by: str | None = None,
    asr_registry: AsrProviderRegistry | None = None,
) -> IngestOutcome:
    """Dispatch a URL or local path into the same ingestion pipeline."""
    candidate = (source or "").strip()
    lowered = candidate.lower()
    if (
        lowered.startswith(("http://", "https://"))
        or "://" in candidate
        or "bilibili.com" in lowered
        or "b23.tv" in lowered
        or candidate.startswith("BV")
    ):
        return await ingest_url(
            candidate,
            config,
            refresh=refresh,
            re_enrich=re_enrich,
            enricher=enricher,
            requested_by=requested_by,
            asr_registry=asr_registry,
        )
    return await ingest_local_file(
        candidate,
        config,
        refresh=refresh,
        re_enrich=re_enrich,
        enricher=enricher,
        requested_by=requested_by,
        asr_registry=asr_registry,
    )


async def ingest_url(
    url: str,
    config: Config,
    *,
    refresh: bool = False,
    re_enrich: bool = False,
    enricher: str | None = None,
    requested_by: str | None = None,
    asr_registry: AsrProviderRegistry | None = None,
) -> IngestOutcome:
    async def resolver(client: httpx.AsyncClient) -> SourceIdentity:
        return await resolve_url(url, client)

    async def prepare(
        client: httpx.AsyncClient,
        identity: SourceIdentity,
        work_dir: Path,
        fetch_options: FetchOptions,
        store: JobStore,
        job: Job,
    ) -> PreparedMedia:
        info = await bilibili.fetch_video_info(client, identity.video_id)
        store.update_status(job.id, JobStatus.CAPTURING_MEDIA)
        media = bilibili.BilibiliMediaProvider(client, WbiKeyCache(client), work_dir)
        audio = await media.fetch_audio(identity, fetch_options)
        return PreparedMedia(
            title=info.title,
            author=info.author,
            duration_s=info.duration_s,
            audio=audio,
            source_payload={"view": info.model_dump()},
        )

    return await _ingest(
        resolver,
        prepare,
        config,
        refresh=refresh,
        re_enrich=re_enrich,
        enricher=enricher,
        requested_by=requested_by,
        asr_registry=asr_registry,
    )


async def ingest_local_file(
    source: str | Path,
    config: Config,
    *,
    refresh: bool = False,
    re_enrich: bool = False,
    enricher: str | None = None,
    requested_by: str | None = None,
    asr_registry: AsrProviderRegistry | None = None,
) -> IngestOutcome:
    media_info: LocalMediaInfo | None = None

    async def resolver(_client: httpx.AsyncClient) -> SourceIdentity:
        nonlocal media_info
        media_info = await asyncio.to_thread(inspect_local_media, source)
        return media_info.identity

    async def prepare(
        _client: httpx.AsyncClient,
        _identity: SourceIdentity,
        work_dir: Path,
        _fetch_options: FetchOptions,
        store: JobStore,
        job: Job,
    ) -> PreparedMedia:
        if media_info is None:  # pragma: no cover - resolver contract guard
            raise ConfigError("local media was not resolved")
        store.update_status(job.id, JobStatus.CAPTURING_MEDIA)
        audio, duration_s = await prepare_local_audio(media_info, work_dir)
        return PreparedMedia(
            title=Path(media_info.original_filename).stem,
            author="Local media",
            duration_s=duration_s,
            audio=audio,
            source_payload=media_info.source_payload(duration_s=duration_s),
        )

    return await _ingest(
        resolver,
        prepare,
        config,
        refresh=refresh,
        re_enrich=re_enrich,
        enricher=enricher,
        requested_by=requested_by,
        asr_registry=asr_registry,
    )


async def _ingest(
    resolve_source: ResolveSource,
    prepare_media: PrepareMedia,
    config: Config,
    *,
    refresh: bool = False,
    re_enrich: bool = False,
    enricher: str | None = None,
    requested_by: str | None = None,
    asr_registry: AsrProviderRegistry | None = None,
) -> IngestOutcome:
    store = JobStore(config.db_path)
    job: Job | None = None
    try:
        try:
            executor_name = config.resolved_enrichment_executor(enricher)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        registry = asr_registry or build_default_asr_registry(
            asr_options=config.asr_options,
            home=config.home,
        )
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=True, max_redirects=5
        ) as client:
            identity = await resolve_source(client)

            existing = store.find_existing(identity.platform, identity.video_id)
            if existing is not None:
                job = existing
            if existing is not None and not refresh:
                if re_enrich:
                    if executor_name == "disabled":
                        raise ConfigError(
                            "--re-enrich requires the api or external_agent executor"
                        )
                    stored = _stored_artifacts(store, existing.id)
                    missing = [
                        kind
                        for kind in (KIND_TRANSCRIPT_JSON, KIND_RAW_MD)
                        if kind not in stored or not Path(stored[kind]).is_file()
                    ]
                    if missing:
                        raise ConfigError(
                            "cannot re-enrich; missing stored artifacts: "
                            + ", ".join(missing)
                        )
                    normalized = NormalizedTranscript.model_validate_json(
                        Path(stored[KIND_TRANSCRIPT_JSON]).read_text(encoding="utf-8")
                    )
                    staging = config.home / "jobs" / existing.id / "artifacts"
                    return await _run_enrichment(
                        store=store,
                        job=existing,
                        config=config,
                        normalized=normalized,
                        raw_path=Path(stored[KIND_RAW_MD]),
                        staging=staging,
                        client=client,
                        executor_name=executor_name,
                    )
                if existing.status == JobStatus.COMPLETED:
                    raise DuplicateJob(
                        f"already ingested: {identity.platform}/{identity.video_id}",
                        job_id=existing.id,
                    )
                task = store.get_enrichment_task(existing.id)
                if task and task["executor"] == "external_agent" and task["status"] in {
                    "pending",
                    "claimed",
                    "failed_retryable",
                }:
                    return IngestOutcome(
                        exit_code=EXIT_COMPLETED,
                        job_id=existing.id,
                        status=existing.status.value,
                        artifacts=_stored_artifacts(store, existing.id),
                        message=f"existing agent enrichment task: {existing.id}",
                    )
            if job is None:
                job = Job(
                    id=uuid.uuid4().hex,
                    platform=identity.platform,
                    video_id=identity.video_id,
                    requested_by=requested_by,
                    destination=config.destination,
                )
                if existing is None:
                    store.create_job(job)

            options = FetchOptions(preferred_languages=config.preferred_languages)
            work_dir = config.home / "jobs" / job.id
            work_dir.mkdir(parents=True, exist_ok=True)
            asr = registry.create(config.asr_provider, client)

            store.update_status(job.id, JobStatus.RESOLVING)
            prepared = await prepare_media(
                client, identity, work_dir, options, store, job
            )

            store.update_status(job.id, JobStatus.TRANSCRIBING)
            asr_timeout = max(150.0, (prepared.duration_s or 0) * 1.5)
            asr_result = await asr.transcribe(
                prepared.audio, AsrOptions(timeout_s=asr_timeout)
            )

            store.update_status(job.id, JobStatus.NORMALIZING)
            normalized = from_asr_result(
                identity,
                title=prepared.title,
                author=prepared.author,
                duration_ms=(
                    int(prepared.duration_s * 1000) if prepared.duration_s else None
                ),
                asr_result=asr_result,
                fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            staging = work_dir / "artifacts"
            source_payload = dict(prepared.source_payload)
            source_payload.update(
                {
                    "asr_provider": asr_result.provider,
                    "asr_model": asr_result.model,
                    "asr_provenance": asr_result.provenance,
                }
            )
            artifacts = write_artifacts(
                staging, source_payload=source_payload, normalized=normalized
            )
            sink = FilesystemSink(config.library_root)
            raw_receipt = await sink.publish(
                artifacts,
                platform=identity.platform,
                video_id=identity.video_id,
            )
            for kind, path in artifacts.items():
                store.add_artifact(
                    job.id,
                    kind,
                    raw_receipt.artifacts[kind],
                    content_hash(path),
                )
            store.update_status(job.id, JobStatus.RAW_PUBLISHED)
            return await _run_enrichment(
                store=store,
                job=job,
                config=config,
                normalized=normalized,
                raw_path=Path(raw_receipt.artifacts[KIND_RAW_MD]),
                staging=staging,
                client=client,
                executor_name=executor_name,
            )
    except DuplicateJob as error:
        artifacts = {a["kind"]: a["path"] for a in store.artifacts(error.job_id or "")}
        return IngestOutcome(
            exit_code=EXIT_DUPLICATE,
            job_id=error.job_id,
            status="duplicate",
            artifacts=artifacts,
            message=str(error),
        )
    except By2kbError as error:
        return _failure(store, job, error)
    finally:
        store.close()
