from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from by2kb.config import Config
from by2kb.errors import (
    By2kbError,
    DuplicateJob,
    UnsupportedUrl,
    category_of,
)
from by2kb.jobs.model import STATUS_FOR_ERROR, Job, JobStatus
from by2kb.jobs.store import JobStore
from by2kb.normalize import from_asr_result
from by2kb.providers import bilibili
from by2kb.providers.asr import AsrOptions
from by2kb.providers.asr_doubao_auc import DoubaoAucAsrProvider, DoubaoAucConfig
from by2kb.providers.base import FetchOptions
from by2kb.providers.bilibili_wbi import WbiKeyCache
from by2kb.sinks.filesystem import FilesystemSink
from by2kb.skills.model import find_skill
from by2kb.skills.runner import OpenAiCompatibleClient, run_skill
from by2kb.writers.raw import RAW_MD, UPDATED_MD, content_hash, write_artifacts
from by2kb.writers.updated import render_updated_md, write_updated_md

EXIT_COMPLETED = 0
EXIT_DUPLICATE = 4


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


def resolve_url(url: str):
    if "bilibili.com" in url or "b23.tv" in url or url.strip().startswith("BV"):
        return bilibili.resolve(url)
    if "youtube.com" in url or "youtu.be" in url:
        raise UnsupportedUrl("YouTube support is not implemented yet (Milestone 1 is Bilibili-first)")
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


async def ingest_url(
    url: str,
    config: Config,
    *,
    refresh: bool = False,
    requested_by: str | None = None,
) -> IngestOutcome:
    store = JobStore(config.db_path)
    job: Job | None = None
    try:
        try:
            identity = resolve_url(url)
        except UnsupportedUrl as error:
            return IngestOutcome(
                exit_code=error.exit_code, status="failed_terminal", message=str(error)
            )

        existing = store.find_existing(identity.platform, identity.video_id)
        if existing is not None and not refresh:
            if existing.status == JobStatus.COMPLETED:
                artifacts = {a["kind"]: a["path"] for a in store.artifacts(existing.id)}
                raise DuplicateJob(
                    f"already ingested: {identity.platform}/{identity.video_id}",
                    job_id=existing.id,
                )
            job = existing
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

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            store.update_status(job.id, JobStatus.RESOLVING)
            info = await bilibili.fetch_video_info(client, identity.video_id)

            store.update_status(job.id, JobStatus.CAPTURING_MEDIA)
            media = bilibili.BilibiliMediaProvider(client, WbiKeyCache(client), work_dir)
            audio = await media.fetch_audio(identity, options)

            store.update_status(job.id, JobStatus.TRANSCRIBING)
            asr = DoubaoAucAsrProvider(DoubaoAucConfig.from_env(), client)
            asr_timeout = max(150.0, (info.duration_s or 0) * 1.5)
            asr_result = await asr.transcribe(audio, AsrOptions(timeout_s=asr_timeout))

            store.update_status(job.id, JobStatus.NORMALIZING)
            normalized = from_asr_result(
                identity,
                title=info.title,
                author=info.author,
                duration_ms=info.duration_s * 1000 if info.duration_s else None,
                asr_result=asr_result,
                fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            staging = work_dir / "artifacts"
            source_payload = {
                "view": info.model_dump(),
                "asr_provenance": asr_result.provenance,
            }
            artifacts = write_artifacts(
                staging, source_payload=source_payload, normalized=normalized
            )
            store.update_status(job.id, JobStatus.RAW_PUBLISHED)

            if config.llm.usable:
                store.update_status(job.id, JobStatus.ENRICHING)
                skill_name = config.skills[0] if config.skills else "default-video-digest"
                skill_dirs = config.skills_dirs or [config.home / "skills"]
                skill = find_skill(skill_name, skill_dirs)
                if skill is not None:
                    llm = OpenAiCompatibleClient(config.llm, client)
                    body = await run_skill(
                        skill, normalized, artifacts[RAW_MD].read_text(encoding="utf-8"), llm
                    )
                    artifacts[UPDATED_MD] = write_updated_md(
                        staging,
                        render_updated_md(
                            normalized,
                            body=body,
                            skill_name=skill.name,
                            skill_version=skill.version,
                            model=llm.model,
                            provider=llm.provider,
                        ),
                    )
                    store.update_status(job.id, JobStatus.UPDATED_PUBLISHED)

            sink = FilesystemSink(config.library_root)
            receipt = await sink.publish(
                artifacts, platform=identity.platform, video_id=identity.video_id
            )
            for kind, path in artifacts.items():
                store.add_artifact(
                    job.id, kind, receipt.artifacts[kind], content_hash(path)
                )
            store.update_status(job.id, JobStatus.COMPLETED)
            return IngestOutcome(
                exit_code=EXIT_COMPLETED,
                job_id=job.id,
                status=JobStatus.COMPLETED.value,
                artifacts=receipt.artifacts,
                message=f"completed: {receipt.target}",
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
