from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from by2kb.providers.base import SourceIdentity

TranscriptKind = Literal["human", "auto_caption", "asr"]


class Segment(BaseModel):
    start_ms: int
    duration_ms: int
    text: str
    confidence: float | None = None


class SourceMeta(BaseModel):
    platform: str
    video_id: str
    canonical_url: str
    title: str
    author: str
    duration_ms: int | None = None


class TranscriptMeta(BaseModel):
    provider: str
    model: str | None = None
    kind: TranscriptKind
    language: str | None = None
    available_languages: list[str] = []
    fetched_at: str
    segments: list[Segment] = []


class NormalizedTranscript(BaseModel):
    schema_version: int = 1
    source: SourceMeta
    transcript: TranscriptMeta


def source_meta_from_identity(
    identity: SourceIdentity,
    *,
    title: str,
    author: str,
    duration_ms: int | None,
) -> SourceMeta:
    return SourceMeta(
        platform=identity.platform,
        video_id=identity.video_id,
        canonical_url=identity.canonical_url,
        title=title,
        author=author,
        duration_ms=duration_ms,
    )


def format_timestamp(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def from_asr_result(
    identity: SourceIdentity,
    *,
    title: str,
    author: str,
    duration_ms: int | None,
    asr_result,
    fetched_at: str,
) -> NormalizedTranscript:
    if asr_result.segments:
        segments = [
            Segment(
                start_ms=int(seg.start * 1000),
                duration_ms=max(0, int((seg.end - seg.start) * 1000)),
                text=seg.text,
            )
            for seg in asr_result.segments
        ]
    elif asr_result.text:
        segments = [
            Segment(start_ms=0, duration_ms=duration_ms or 0, text=asr_result.text)
        ]
    else:
        segments = []
    return NormalizedTranscript(
        source=source_meta_from_identity(
            identity, title=title, author=author, duration_ms=duration_ms
        ),
        transcript=TranscriptMeta(
            provider=asr_result.provider,
            model=asr_result.model,
            kind="asr",
            language=asr_result.language,
            fetched_at=fetched_at,
            segments=segments,
        ),
    )
