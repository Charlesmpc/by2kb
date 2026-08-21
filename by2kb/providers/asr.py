from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from by2kb.providers.base import LocalAudio


class AsrSegment(BaseModel):
    start: float
    end: float
    text: str


class AsrOptions(BaseModel):
    timeout_s: float = 150.0
    language: str | None = None


class AsrResult(BaseModel):
    provider: str
    model: str
    language: str | None = None
    text: str
    segments: list[AsrSegment] = []
    provenance: dict = {}


@runtime_checkable
class AsrProvider(Protocol):
    name: str

    async def transcribe(self, audio: LocalAudio, options: AsrOptions) -> AsrResult: ...
