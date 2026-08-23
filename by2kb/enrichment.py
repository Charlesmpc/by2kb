from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from by2kb.config import Config
from by2kb.errors import ConfigError
from by2kb.filenames import markdown_artifact_name
from by2kb.normalize import NormalizedTranscript
from by2kb.skills.model import Skill, find_skill
from by2kb.skills.runner import LlmClient, build_prompts, run_skill
from by2kb.writers.updated import render_updated_md, write_updated_md

KIND_ABSTRACT_MD = "abstract_md"
KIND_UPDATED_MD = "updated_md"


@dataclass(frozen=True)
class EnrichmentRequest:
    job_id: str
    normalized: NormalizedTranscript
    raw_path: Path
    staging: Path
    abstract_skill: Skill
    study_skill: Skill


@dataclass(frozen=True)
class EnrichmentSubmission:
    deferred: bool
    artifacts: dict[str, Path] = field(default_factory=dict)


@runtime_checkable
class EnrichmentExecutor(Protocol):
    name: str

    async def submit(self, request: EnrichmentRequest) -> EnrichmentSubmission: ...


class ApiEnrichmentExecutor:
    name = "api"

    def __init__(self, llm: LlmClient):
        self._llm = llm

    async def submit(self, request: EnrichmentRequest) -> EnrichmentSubmission:
        generated: dict[str, Path] = {}
        for kind, filename_kind, artifact_type, skill in _outputs(request):
            body = await run_skill(
                skill,
                request.normalized,
                request.raw_path.read_text(encoding="utf-8"),
                self._llm,
            )
            generated[kind] = _write_output(
                request,
                body=body,
                filename_kind=filename_kind,
                artifact_type=artifact_type,
                skill=skill,
                provider=self._llm.provider,
                model=self._llm.model,
            )
        return EnrichmentSubmission(deferred=False, artifacts=generated)


class ExternalAgentEnrichmentExecutor:
    name = "external_agent"

    async def submit(self, request: EnrichmentRequest) -> EnrichmentSubmission:
        del request
        return EnrichmentSubmission(deferred=True)


class DisabledEnrichmentExecutor:
    name = "disabled"

    async def submit(self, request: EnrichmentRequest) -> EnrichmentSubmission:
        del request
        return EnrichmentSubmission(deferred=False)


def create_enrichment_request(
    config: Config,
    *,
    job_id: str,
    normalized: NormalizedTranscript,
    raw_path: Path,
    staging: Path,
    abstract_profile: str | None = None,
    study_profile: str | None = None,
) -> EnrichmentRequest:
    skill_dirs = config.skills_dirs or [config.home / "skills"]
    abstract_name = abstract_profile or config.abstract_skill
    study_name = study_profile or config.study_skill or (
        config.skills[0] if config.skills else "default-video-digest"
    )
    abstract_skill = find_skill(abstract_name, skill_dirs)
    study_skill = find_skill(study_name, skill_dirs)
    missing = [
        name
        for name, skill in (
            (abstract_name, abstract_skill),
            (study_name, study_skill),
        )
        if skill is None
    ]
    if missing:
        raise ConfigError("summary skill not found: " + ", ".join(missing))
    return EnrichmentRequest(
        job_id=job_id,
        normalized=normalized,
        raw_path=raw_path,
        staging=staging,
        abstract_skill=abstract_skill,
        study_skill=study_skill,
    )


def external_manifest(request: EnrichmentRequest) -> dict:
    raw_md = request.raw_path.read_text(encoding="utf-8")
    outputs: dict[str, dict] = {}
    for kind, _filename_kind, artifact_type, skill in _outputs(request):
        system, user = build_prompts(skill, request.normalized, raw_md)
        outputs[kind] = {
            "artifact_type": artifact_type,
            "profile": skill.name,
            "profile_version": skill.version,
            "system_prompt": system,
            "user_prompt": user,
        }
    return {
        "job_id": request.job_id,
        "raw_path": str(request.raw_path),
        "source": request.normalized.source.model_dump(mode="json"),
        "outputs": outputs,
    }


def write_external_artifacts(
    request: EnrichmentRequest,
    *,
    abstract_body: str,
    study_body: str,
    provider: str,
    model: str,
) -> dict[str, Path]:
    if not abstract_body.strip() or not study_body.strip():
        raise ConfigError("external enrichment outputs must not be empty")
    generated: dict[str, Path] = {}
    bodies = {
        KIND_ABSTRACT_MD: abstract_body,
        KIND_UPDATED_MD: study_body,
    }
    for kind, filename_kind, artifact_type, skill in _outputs(request):
        generated[kind] = _write_output(
            request,
            body=bodies[kind],
            filename_kind=filename_kind,
            artifact_type=artifact_type,
            skill=skill,
            provider=provider,
            model=model,
        )
    return generated


def _outputs(request: EnrichmentRequest):
    return (
        (KIND_ABSTRACT_MD, "abstract", "short_abstract", request.abstract_skill),
        (KIND_UPDATED_MD, "updated", "study_notes", request.study_skill),
    )


def _write_output(
    request: EnrichmentRequest,
    *,
    body: str,
    filename_kind: str,
    artifact_type: str,
    skill: Skill,
    provider: str,
    model: str,
) -> Path:
    output_name = markdown_artifact_name(
        request.normalized.source.title,
        request.normalized.source.video_id,
        filename_kind,
    )
    return write_updated_md(
        request.staging,
        render_updated_md(
            request.normalized,
            body=body,
            skill_name=skill.name,
            skill_version=skill.version,
            model=model,
            provider=provider,
            artifact_type=artifact_type,
            raw_ref=request.raw_path.name,
        ),
        filename=output_name,
    )
