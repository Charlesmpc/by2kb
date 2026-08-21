from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class SinkReceipt(BaseModel):
    sink: str
    target: str
    artifacts: dict[str, str] = {}


@runtime_checkable
class KnowledgeSink(Protocol):
    name: str

    async def publish(
        self, artifact_paths: dict[str, object], *, platform: str, video_id: str
    ) -> SinkReceipt: ...
