from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from by2kb.config import LongFormConfig
from by2kb.errors import ConfigError
from by2kb.normalize import NormalizedTranscript, Segment, format_timestamp

if TYPE_CHECKING:
    from by2kb.enrichment import EnrichmentRequest
    from by2kb.skills.runner import LlmClient

PIPELINE_VERSION = "1.0"
CACHE_SCHEMA_VERSION = 1
KIND_ENRICHMENT_PLAN_JSON = "enrichment_plan_json"


class ChunkSpec(BaseModel):
    id: str
    first_segment: int
    last_segment: int
    start_ms: int
    end_ms: int
    estimated_tokens: int
    input_hash: str
    oversized: bool = False


class EnrichmentPlan(BaseModel):
    pipeline_version: str = PIPELINE_VERSION
    strategy: str
    transcript_hash: str
    estimated_tokens: int
    threshold_tokens: int
    chunk_token_budget: int
    chunk_duration_s: int
    reduce_token_budget: int
    reduce_group_size: int
    chunks: list[ChunkSpec]

    @property
    def plan_hash(self) -> str:
        return _digest(self.model_dump(mode="json"))


class TraceNode(BaseModel):
    id: str
    level: int
    source_chunks: list[str]
    start_ms: int
    end_ms: int
    cache_key: str
    cache_hit: bool
    child_ids: list[str] = Field(default_factory=list)


class EnrichmentTrace(BaseModel):
    schema_version: int = 1
    pipeline_version: str = PIPELINE_VERSION
    strategy: str
    plan_hash: str
    transcript_hash: str
    runtime_provider: str
    runtime_model: str
    runtime_version: str = ""
    skills: list[str]
    chunks: list[ChunkSpec]
    hierarchy: list[TraceNode]
    cache_hits: int
    cache_misses: int


@dataclass(frozen=True)
class LongFormResult:
    context: str
    trace: EnrichmentTrace


@dataclass(frozen=True)
class _SummaryNode:
    id: str
    text: str
    source_chunks: tuple[str, ...]
    start_ms: int
    end_ms: int
    cache_key: str
    level: int


class TranscriptChunkPlanner:
    def __init__(self, config: LongFormConfig):
        _validate_config(config)
        self._config = config

    def plan(self, normalized: NormalizedTranscript) -> EnrichmentPlan:
        segments = normalized.transcript.segments
        rendered = [_render_segment(segment) for segment in segments]
        total_tokens = sum(estimate_tokens(text) for text in rendered)
        transcript_hash = _digest(
            {
                "schema_version": normalized.schema_version,
                "source": {
                    "platform": normalized.source.platform,
                    "video_id": normalized.source.video_id,
                    "duration_ms": normalized.source.duration_ms,
                },
                "transcript": {
                    "provider": normalized.transcript.provider,
                    "model": normalized.transcript.model,
                    "kind": normalized.transcript.kind,
                    "language": normalized.transcript.language,
                    "segments": [
                        segment.model_dump(mode="json")
                        for segment in normalized.transcript.segments
                    ],
                },
            }
        )
        if total_tokens <= self._config.threshold_tokens:
            return EnrichmentPlan(
                strategy="single_pass",
                transcript_hash=transcript_hash,
                estimated_tokens=total_tokens,
                threshold_tokens=self._config.threshold_tokens,
                chunk_token_budget=self._config.chunk_token_budget,
                chunk_duration_s=self._config.chunk_duration_s,
                reduce_token_budget=self._config.reduce_token_budget,
                reduce_group_size=self._config.reduce_group_size,
                chunks=[],
            )

        chunks: list[ChunkSpec] = []
        current: list[int] = []
        current_tokens = 0
        for index, (segment, text) in enumerate(zip(segments, rendered, strict=True)):
            segment_tokens = estimate_tokens(text)
            proposed = [*current, index]
            proposed_duration = _chunk_duration_ms(segments, proposed)
            exceeds = (
                current
                and (
                    current_tokens + segment_tokens
                    > self._config.chunk_token_budget
                    or proposed_duration > self._config.chunk_duration_s * 1000
                )
            )
            if exceeds:
                chunks.append(self._chunk_spec(segments, rendered, current, len(chunks)))
                current = []
                current_tokens = 0
            current.append(index)
            current_tokens += segment_tokens
        if current:
            chunks.append(self._chunk_spec(segments, rendered, current, len(chunks)))

        return EnrichmentPlan(
            strategy="hierarchical",
            transcript_hash=transcript_hash,
            estimated_tokens=total_tokens,
            threshold_tokens=self._config.threshold_tokens,
            chunk_token_budget=self._config.chunk_token_budget,
            chunk_duration_s=self._config.chunk_duration_s,
            reduce_token_budget=self._config.reduce_token_budget,
            reduce_group_size=self._config.reduce_group_size,
            chunks=chunks,
        )

    def _chunk_spec(
        self,
        segments: list[Segment],
        rendered: list[str],
        indices: list[int],
        ordinal: int,
    ) -> ChunkSpec:
        first = indices[0]
        last = indices[-1]
        text = "\n".join(rendered[index] for index in indices)
        tokens = estimate_tokens(text)
        start_ms = segments[first].start_ms
        end_ms = max(
            segment.start_ms + segment.duration_ms
            for segment in segments[first : last + 1]
        )
        return ChunkSpec(
            id=f"chunk-{ordinal:04d}",
            first_segment=first,
            last_segment=last,
            start_ms=start_ms,
            end_ms=end_ms,
            estimated_tokens=tokens,
            input_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            oversized=tokens > self._config.chunk_token_budget,
        )


class IntermediateCache:
    def __init__(self, root: Path):
        self._root = root

    def get(self, key: str) -> str | None:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if (
            payload.get("schema_version") != CACHE_SCHEMA_VERSION
            or payload.get("cache_key") != key
            or not isinstance(payload.get("content"), str)
            or not payload["content"].strip()
        ):
            return None
        return payload["content"]

    def put(self, key: str, content: str, metadata: dict[str, object]) -> None:
        if not content.strip():
            raise ConfigError("long-form intermediate summary must not be empty")
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "cache_key": key,
                    "metadata": metadata,
                    "content": content,
                },
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / f"{key}.json"


class LongFormEnrichmentPipeline:
    def __init__(self, config: LongFormConfig, cache_root: Path):
        self._config = config
        self._planner = TranscriptChunkPlanner(config)
        self._cache = IntermediateCache(cache_root)

    def plan(self, request: "EnrichmentRequest") -> EnrichmentPlan:
        return self._planner.plan(request.normalized)

    async def run(
        self,
        request: "EnrichmentRequest",
        llm: "LlmClient",
    ) -> LongFormResult:
        plan = self.plan(request)
        skills = [
            _skill_identity(request.abstract_skill),
            _skill_identity(request.study_skill),
        ]
        if plan.strategy == "single_pass":
            trace = EnrichmentTrace(
                strategy=plan.strategy,
                plan_hash=plan.plan_hash,
                transcript_hash=plan.transcript_hash,
                runtime_provider=llm.provider,
                runtime_model=llm.model,
                runtime_version=getattr(llm, "runtime_version", ""),
                skills=skills,
                chunks=[],
                hierarchy=[],
                cache_hits=0,
                cache_misses=0,
            )
            return LongFormResult(
                context=request.raw_path.read_text(encoding="utf-8"),
                trace=trace,
            )

        nodes: list[_SummaryNode] = []
        trace_nodes: list[TraceNode] = []
        cache_hits = 0
        cache_misses = 0
        for chunk in plan.chunks:
            if request.cancel_check:
                request.cancel_check()
            content = _chunk_text(request.normalized, chunk)
            key = _cache_key(
                stage="chunk",
                inputs=[chunk.input_hash],
                llm=llm,
                skills=skills,
                plan=plan,
            )
            summary = self._cache.get(key)
            hit = summary is not None
            if hit:
                cache_hits += 1
            else:
                cache_misses += 1
                system, user = chunk_prompt(request.normalized, chunk, content)
                summary = await llm.complete(system, user)
                self._cache.put(
                    key,
                    summary,
                    {
                        "stage": "chunk",
                        "chunk_id": chunk.id,
                        "input_hash": chunk.input_hash,
                        "runtime_provider": llm.provider,
                        "runtime_model": llm.model,
                        "runtime_version": getattr(llm, "runtime_version", ""),
                        "skills": skills,
                        "pipeline_version": PIPELINE_VERSION,
                    },
                )
            node = _SummaryNode(
                id=chunk.id,
                text=summary,
                source_chunks=(chunk.id,),
                start_ms=chunk.start_ms,
                end_ms=chunk.end_ms,
                cache_key=key,
                level=0,
            )
            nodes.append(node)
            trace_nodes.append(
                TraceNode(
                    id=node.id,
                    level=0,
                    source_chunks=list(node.source_chunks),
                    start_ms=node.start_ms,
                    end_ms=node.end_ms,
                    cache_key=key,
                    cache_hit=hit,
                )
            )

        level = 1
        while len(nodes) > 1:
            next_nodes: list[_SummaryNode] = []
            for ordinal, group in enumerate(self._groups(nodes)):
                if request.cancel_check:
                    request.cancel_check()
                if len(group) == 1:
                    next_nodes.append(group[0])
                    continue
                key = _cache_key(
                    stage=f"reduce-{level}",
                    inputs=[node.cache_key for node in group],
                    llm=llm,
                    skills=skills,
                    plan=plan,
                )
                summary = self._cache.get(key)
                hit = summary is not None
                if hit:
                    cache_hits += 1
                else:
                    cache_misses += 1
                    system, user = reduction_prompt(group, level)
                    summary = await llm.complete(system, user)
                    self._cache.put(
                        key,
                        summary,
                        {
                            "stage": "reduce",
                            "level": level,
                            "children": [node.id for node in group],
                            "runtime_provider": llm.provider,
                            "runtime_model": llm.model,
                            "runtime_version": getattr(llm, "runtime_version", ""),
                            "skills": skills,
                            "pipeline_version": PIPELINE_VERSION,
                        },
                    )
                node = _SummaryNode(
                    id=f"reduce-{level}-{ordinal:04d}",
                    text=summary,
                    source_chunks=tuple(
                        chunk
                        for child in group
                        for chunk in child.source_chunks
                    ),
                    start_ms=min(child.start_ms for child in group),
                    end_ms=max(child.end_ms for child in group),
                    cache_key=key,
                    level=level,
                )
                next_nodes.append(node)
                trace_nodes.append(
                    TraceNode(
                        id=node.id,
                        level=level,
                        source_chunks=list(node.source_chunks),
                        start_ms=node.start_ms,
                        end_ms=node.end_ms,
                        cache_key=key,
                        cache_hit=hit,
                        child_ids=[child.id for child in group],
                    )
                )
            nodes = next_nodes
            level += 1

        root = nodes[0]
        trace = EnrichmentTrace(
            strategy=plan.strategy,
            plan_hash=plan.plan_hash,
            transcript_hash=plan.transcript_hash,
            runtime_provider=llm.provider,
            runtime_model=llm.model,
            runtime_version=getattr(llm, "runtime_version", ""),
            skills=skills,
            chunks=plan.chunks,
            hierarchy=trace_nodes,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        )
        context = (
            "# Hierarchically reduced grounded transcript notes\n\n"
            f"Source range: {format_timestamp(root.start_ms)}–"
            f"{format_timestamp(root.end_ms)}\n\n{root.text.strip()}\n"
        )
        return LongFormResult(context=context, trace=trace)

    def _groups(self, nodes: list[_SummaryNode]) -> list[list[_SummaryNode]]:
        groups: list[list[_SummaryNode]] = []
        index = 0
        while index < len(nodes):
            group = [nodes[index]]
            index += 1
            while index < len(nodes) and len(group) < self._config.reduce_group_size:
                candidate = nodes[index]
                combined = "\n\n".join([*(node.text for node in group), candidate.text])
                if (
                    len(group) >= 2
                    and estimate_tokens(combined) > self._config.reduce_token_budget
                ):
                    break
                group.append(candidate)
                index += 1
            groups.append(group)
        if len(groups) == len(nodes) and len(nodes) > 1:
            return [nodes[index : index + 2] for index in range(0, len(nodes), 2)]
        return groups


def write_enrichment_trace(staging: Path, trace: EnrichmentTrace) -> Path:
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / "enrichment-plan.json"
    path.write_text(trace.model_dump_json(indent=1) + "\n", encoding="utf-8")
    return path


def estimate_tokens(text: str) -> int:
    cjk = sum(
        1
        for character in text
        if "\u3400" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
    )
    non_cjk = sum(1 for character in text if not character.isspace()) - cjk
    return cjk + math.ceil(max(0, non_cjk) / 4)


def chunk_prompt(
    normalized: NormalizedTranscript,
    chunk: ChunkSpec,
    content: str,
) -> tuple[str, str]:
    system = (
        "Produce grounded intermediate notes from one transcript chunk. Preserve "
        "important claims and timestamp references. Do not infer missing context or "
        "write a final abstract."
    )
    user = (
        f"# Source\n\n{normalized.source.title}\n\n"
        f"# Chunk\n\n- id: {chunk.id}\n"
        f"- source range: {format_timestamp(chunk.start_ms)}–"
        f"{format_timestamp(chunk.end_ms)}\n"
        f"- segment range: {chunk.first_segment}–{chunk.last_segment}\n\n"
        f"# Transcript segments\n\n{content}"
    )
    return system, user


def reduction_prompt(
    nodes: list[_SummaryNode],
    level: int,
) -> tuple[str, str]:
    system = (
        "Merge grounded intermediate transcript notes into a smaller grounded set. "
        "Preserve source ranges, disagreements, uncertainty, and timestamp references. "
        "Do not add facts or hidden reasoning."
    )
    sections = []
    for node in nodes:
        sections.append(
            f"## {node.id} [{format_timestamp(node.start_ms)}–"
            f"{format_timestamp(node.end_ms)}]\n\n{node.text.strip()}"
        )
    return system, f"# Reduction level {level}\n\n" + "\n\n".join(sections)


def _chunk_text(normalized: NormalizedTranscript, chunk: ChunkSpec) -> str:
    return "\n".join(
        _render_segment(segment)
        for segment in normalized.transcript.segments[
            chunk.first_segment : chunk.last_segment + 1
        ]
    )


def _render_segment(segment: Segment) -> str:
    return f"[{format_timestamp(segment.start_ms)}] {segment.text.strip()}"


def _chunk_duration_ms(segments: list[Segment], indices: list[int]) -> int:
    first = segments[indices[0]]
    end = max(
        segments[index].start_ms + segments[index].duration_ms for index in indices
    )
    return max(0, end - first.start_ms)


def _cache_key(
    *,
    stage: str,
    inputs: list[str],
    llm: "LlmClient",
    skills: list[str],
    plan: EnrichmentPlan,
) -> str:
    return _digest(
        {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "stage": stage,
            "inputs": inputs,
            "runtime_provider": llm.provider,
            "runtime_model": llm.model,
            "runtime_version": getattr(llm, "runtime_version", ""),
            "skills": skills,
            "plan_hash": plan.plan_hash,
        }
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _skill_identity(skill) -> str:
    body_hash = hashlib.sha256(skill.body.encode("utf-8")).hexdigest()[:12]
    return f"{skill.name}@{skill.version}:{body_hash}"


def _validate_config(config: LongFormConfig) -> None:
    values = {
        "threshold_tokens": config.threshold_tokens,
        "chunk_token_budget": config.chunk_token_budget,
        "chunk_duration_s": config.chunk_duration_s,
        "reduce_token_budget": config.reduce_token_budget,
        "reduce_group_size": config.reduce_group_size,
    }
    invalid = [name for name, value in values.items() if value <= 0]
    if invalid:
        raise ConfigError(
            "long-form enrichment settings must be positive: " + ", ".join(invalid)
        )
