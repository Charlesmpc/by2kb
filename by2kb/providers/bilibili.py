from __future__ import annotations

import re

import httpx
from pydantic import BaseModel

from by2kb.errors import (
    NeedsAuth,
    RateLimited,
    TerminalProviderError,
    TransientProviderError,
    UnsupportedUrl,
)
from by2kb.providers.base import FetchOptions, LocalAudio, SourceIdentity
from by2kb.providers.bilibili_wbi import WbiKeyCache, signed_url

API_BASE = "https://api.bilibili.com"
VIEW_URL = f"{API_BASE}/x/web-interface/view"
PLAYURL_URL = f"{API_BASE}/x/player/wbi/playurl"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
TERMINAL_CODES = {-404: "video not found", 62002: "video unavailable", 62004: "video unavailable"}


class BilibiliVideoInfo(BaseModel):
    bvid: str
    aid: int
    cid: int
    title: str
    author: str
    duration_s: int
    page: int = 1
    page_count: int = 1


def resolve(url: str) -> SourceIdentity:
    match = BVID_PATTERN.search(url or "")
    if not match:
        raise UnsupportedUrl(f"not a recognizable Bilibili video URL: {url}")
    bvid = match.group(0)
    return SourceIdentity(
        platform="bilibili",
        video_id=bvid,
        canonical_url=f"https://www.bilibili.com/video/{bvid}/",
    )


async def expand_short_url(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(url, headers=_headers(), follow_redirects=False)
    except httpx.HTTPError as exc:
        raise TransientProviderError(
            f"short link expansion failed: {exc}", provider="bilibili"
        ) from exc
    location = response.headers.get("location")
    if response.status_code not in range(300, 400) or not location:
        raise TransientProviderError(
            f"short link expansion failed: HTTP {response.status_code}",
            provider="bilibili",
        )
    return str(httpx.URL(url).join(location))


def _headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.bilibili.com",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def read_envelope(payload: dict, *, what: str, provider: str = "bilibili") -> dict:
    code = payload.get("code")
    if code == 0:
        return payload.get("data") or {}
    message = payload.get("message") or f"business code {code}"
    if code in TERMINAL_CODES:
        raise TerminalProviderError(f"{what}: {message}", provider=provider, detail=code)
    if code == -403:
        raise NeedsAuth(f"{what}: {message}", provider=provider, detail=code)
    if code == -352:
        raise RateLimited(f"{what}: risk control ({message})", provider=provider, detail=code)
    raise TerminalProviderError(f"{what}: {message}", provider=provider, detail=code)


async def fetch_video_info(
    client: httpx.AsyncClient, bvid: str, *, page: int = 1
) -> BilibiliVideoInfo:
    try:
        response = await client.get(
            VIEW_URL, params={"bvid": bvid}, headers=_headers()
        )
    except httpx.HTTPError as exc:
        raise TransientProviderError(f"view failed: {exc}", provider="bilibili") from exc
    if response.status_code != 200:
        raise TransientProviderError(
            f"view failed: HTTP {response.status_code}", provider="bilibili"
        )
    data = read_envelope(response.json(), what="view")

    pages = data.get("pages") or []
    target = next((p for p in pages if p.get("page") == page), None) or (pages[0] if pages else {})
    cid = int(target.get("cid") or data.get("cid") or 0)
    if not cid:
        raise TerminalProviderError("view returned no cid", provider="bilibili")
    title = (target.get("part") if len(pages) > 1 else None) or data.get("title") or ""
    return BilibiliVideoInfo(
        bvid=str(data.get("bvid") or bvid),
        aid=int(data.get("aid") or 0),
        cid=cid,
        title=str(title),
        author=str((data.get("owner") or {}).get("name") or ""),
        duration_s=int(target.get("duration") or data.get("duration") or 0),
        page=page,
        page_count=max(len(pages), 1),
    )


class BilibiliMediaProvider:
    platform = "bilibili"

    def __init__(
        self,
        client: httpx.AsyncClient,
        keys: WbiKeyCache,
        work_dir,
    ):
        self._client = client
        self._keys = keys
        self._work_dir = work_dir

    async def fetch_audio(
        self, identity: SourceIdentity, options: FetchOptions
    ) -> LocalAudio:
        info = await fetch_video_info(self._client, identity.video_id)
        referer = identity.canonical_url
        img_key, sub_key = await self._keys.get_keys()
        params = {
            "avid": info.aid,
            "cid": info.cid,
            "bvid": info.bvid,
            "fnval": 4048,
            "fnver": 0,
            "fourk": 1,
            "platform": "pc",
        }
        url = signed_url(PLAYURL_URL, params, img_key, sub_key)
        try:
            response = await self._client.get(url, headers=_headers(referer))
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"playurl failed: {exc}", provider="bilibili") from exc
        if response.status_code != 200:
            raise TransientProviderError(
                f"playurl failed: HTTP {response.status_code}", provider="bilibili"
            )
        data = read_envelope(response.json(), what="playurl")

        audios = (data.get("dash") or {}).get("audio") or []
        if not audios:
            raise TerminalProviderError(
                "playurl returned no audio streams", provider="bilibili"
            )
        best = max(audios, key=lambda a: int(a.get("bandwidth") or 0))
        target = self._work_dir / f"{info.bvid}.m4a"
        await self._download(best["baseUrl"], target, referer)
        return LocalAudio(
            path=target,
            format="mp4",
            duration_s=float(info.duration_s) if info.duration_s else None,
            size_bytes=target.stat().st_size,
        )

    async def _download(self, url: str, target, referer: str) -> None:
        headers = _headers(referer)
        headers["Referer"] = referer
        try:
            async with self._client.stream("GET", url, headers=headers) as stream:
                if stream.status_code not in (200, 206):
                    raise TransientProviderError(
                        f"audio download failed: HTTP {stream.status_code}",
                        provider="bilibili",
                    )
                with open(target, "wb") as fh:
                    async for chunk in stream.aiter_bytes(262144):
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise TransientProviderError(
                f"audio download failed: {exc}", provider="bilibili"
            ) from exc
