from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from by2kb.providers import bilibili
from by2kb.providers.base import (
    FetchOptions,
    PreparedSource,
    SourceIdentity,
)
from by2kb.providers.bilibili_wbi import WbiKeyCache


class BilibiliSourceProvider:
    name = "bilibili_native"

    def supports(self, source: str) -> bool:
        candidate = (source or "").strip()
        lowered = candidate.lower()
        return (
            "bilibili.com" in lowered
            or "b23.tv" in lowered
            or candidate.startswith("BV")
        )

    async def resolve(
        self, source: str, client: httpx.AsyncClient
    ) -> SourceIdentity:
        candidate = source.strip()
        if "b23.tv" in candidate.lower():
            candidate = await bilibili.expand_short_url(client, candidate)
        return bilibili.resolve(candidate)

    async def prepare(
        self,
        identity: SourceIdentity,
        client: httpx.AsyncClient,
        work_dir: Path,
        options: FetchOptions,
        *,
        set_stage: Callable[[str], None],
        cancel_check: Callable[[], None],
    ) -> PreparedSource:
        info = await bilibili.fetch_video_info(client, identity.video_id)
        cancel_check()
        set_stage("capturing_media")
        media = bilibili.BilibiliMediaProvider(client, WbiKeyCache(client), work_dir)
        audio = await media.fetch_audio(identity, options)
        return PreparedSource(
            title=info.title,
            author=info.author,
            duration_s=info.duration_s,
            audio=audio,
            source_payload={
                "source_provider": self.name,
                "view": info.model_dump(),
            },
        )
