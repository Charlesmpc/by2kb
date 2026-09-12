from __future__ import annotations

import asyncio
from urllib.parse import urlparse
import base64
import re
import secrets

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
    metadata_source: str = "view"


def resolve(url: str) -> SourceIdentity:
    parsed = urlparse(url or "")
    candidate = url if BVID_PATTERN.fullmatch(url or "") else (
        parsed.path.removeprefix("/video/").rstrip("/")
        if parsed.scheme in {"http", "https"} and parsed.hostname in
        {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}
        and not parsed.username and not parsed.password and parsed.path.startswith("/video/") else ""
    )
    match = BVID_PATTERN.fullmatch(candidate)
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
        async with asyncio.timeout(10):
            for _ in range(5):
                parsed = urlparse(url)
                if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                        or parsed.hostname != "b23.tv"):
                    return resolve(url).canonical_url
                response = await client.get(url, headers=_headers(), follow_redirects=False)
                location = response.headers.get("location")
                if response.status_code not in {301, 302, 303, 307, 308} or not location:
                    raise TransientProviderError("short link expansion failed", provider="bilibili")
                url = str(httpx.URL(url).join(location))
                if urlparse(url).hostname != "b23.tv":
                    return resolve(url).canonical_url
            raise UnsupportedUrl("short link exceeded 5 redirects")
    except (httpx.HTTPError, TimeoutError) as exc:
        raise TransientProviderError("short link expansion failed or timed out", provider="bilibili") from exc


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
            VIEW_URL, params={"bvid": bvid},
            headers=_headers(f"https://www.bilibili.com/video/{bvid}/"),
        )
    except httpx.HTTPError as exc:
        raise TransientProviderError(f"view failed: {exc}", provider="bilibili") from exc
    if response.status_code != 200:
        if response.status_code == 412:
            try:
                fallback = await client.get(
                    f"{API_BASE}/x/player/pagelist", params={"bvid": bvid},
                    headers=_headers(f"https://www.bilibili.com/video/{bvid}/"),
                )
            except httpx.HTTPError as exc:
                raise TransientProviderError("pagelist metadata unavailable", provider="bilibili") from exc
            if fallback.status_code != 200:
                raise RateLimited("view HTTP 412; pagelist metadata unavailable; try browser or local file", provider="bilibili")
            pages = read_envelope(fallback.json(), what="pagelist")
            target = next((p for p in pages if p.get("page") == page), None)
            if not target or not target.get("cid"):
                raise TerminalProviderError("pagelist returned no requested cid", provider="bilibili")
            return BilibiliVideoInfo(bvid=bvid, aid=0, cid=target["cid"], title="", author="",
                duration_s=target.get("duration") or 0, page=page, page_count=len(pages),
                metadata_source="pagelist_after_view_412")
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


def playback_client_params() -> dict[str, str | int]:
    """Compatibility fields for the WBI playback gateway, added before signing.

    These are synthetic placeholders, not collected user/browser fingerprints.
    See https://github.com/JefferyHcool/BiliNote/issues/397.
    They do not address rejections of the separate metadata view endpoint.
    """
    def encoded_placeholder() -> str:
        return base64.b64encode(secrets.token_hex(16).encode()).decode().rstrip("=")

    return {
        "web_location": 1550101,
        "dm_img_list": "[]",
        "dm_img_str": encoded_placeholder(),
        "dm_cover_img_str": encoded_placeholder(),
        "dm_img_inter": '{"ds":[],"wh":[6093,6631,31],"of":[430,760,380]}',
    }


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
        self.set_stage = lambda stage: None
        self.cancel_check = lambda: None

    async def fetch_audio(
        self, identity: SourceIdentity, options: FetchOptions, *, info: BilibiliVideoInfo | None = None
    ) -> LocalAudio:
        info = info or await fetch_video_info(self._client, identity.video_id)
        referer = identity.canonical_url
        img_key, sub_key = await self._keys.get_keys()
        params = {
            **playback_client_params(),
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
            raise TransientProviderError(
                "playurl returned no audio streams", provider="bilibili"
            )
        best = max(audios, key=lambda a: int(a.get("bandwidth") or 0))
        target = self._work_dir / f"{info.bvid}.m4a"
        audio_urls = [best["baseUrl"], *(best.get("backupUrl") or [])]
        self.set_stage("downloading_media")
        await self._download(audio_urls, target, referer)
        self.set_stage("validating_media")
        from by2kb.providers.browser_source import probe_duration, validate_audio
        try:
            async with asyncio.timeout(30):
                self.cancel_check()
                duration = await probe_duration(target)
                if info.duration_s and (duration is None or duration + 5 < info.duration_s * 0.95):
                    raise TransientProviderError("Incomplete audio; not evidence of a login requirement", provider="bilibili")
                await validate_audio(target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return LocalAudio(
            path=target,
            format="mp4",
            duration_s=float(info.duration_s) if info.duration_s else None,
            size_bytes=target.stat().st_size,
        )

    async def _download(self, urls: list[str], target, referer: str) -> None:
        from by2kb.providers.acquisition import download
        await download(self._client, urls, target, _headers(referer), cancel_check=self.cancel_check)
