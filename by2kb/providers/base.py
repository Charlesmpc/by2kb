from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx

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


@dataclass(frozen=True)
class PreparedSource:
    title: str
    author: str
    duration_s: float | None
    source_payload: dict[str, object]
    audio: LocalAudio | None = None
    transcript: "NormalizedTranscript | None" = None

    def __post_init__(self) -> None:
        if (self.audio is None) == (self.transcript is None):
            raise ValueError("prepared source must contain exactly one of audio or transcript")


@runtime_checkable
class SourceProvider(Protocol):
    name: str

    def supports(self, source: str) -> bool: ...

    async def resolve(
        self, source: str, client: httpx.AsyncClient
    ) -> SourceIdentity: ...

    async def prepare(
        self,
        identity: SourceIdentity,
        client: httpx.AsyncClient,
        work_dir: Path,
        options: FetchOptions,
        *,
        set_stage: Callable[[str], None],
        cancel_check: Callable[[], None],
    ) -> PreparedSource: ...


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
