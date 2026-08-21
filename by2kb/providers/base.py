from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from by2kb.normalize import NormalizedTranscript


class SourceIdentity(BaseModel):
    platform: str
    video_id: str
    canonical_url: str


class FetchOptions(BaseModel):
    preferred_languages: list[str] = ["zh-CN", "zh", "en"]
    allow_audio_fallback: bool = True


class LocalAudio(BaseModel):
    path: Path
    format: str
    duration_s: float | None = None
    size_bytes: int


@runtime_checkable
class TranscriptProvider(Protocol):
    platform: str

    def resolve(self, url: str) -> SourceIdentity: ...

    async def fetch(
        self, identity: SourceIdentity, options: FetchOptions
    ) -> "NormalizedTranscript": ...


@runtime_checkable
class MediaProvider(Protocol):
    platform: str

    async def fetch_audio(
        self, identity: SourceIdentity, options: FetchOptions
    ) -> LocalAudio: ...
