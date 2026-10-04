from __future__ import annotations

import asyncio
import json
import sys
from typing import Protocol
from urllib.parse import urlparse

import httpx

from by2kb.errors import ConfigError
from by2kb.normalize import NormalizedTranscript, Segment, SourceMeta, TranscriptMeta
from by2kb.providers.base import FetchOptions
from by2kb.providers.bilibili import API_BASE, _headers, fetch_video_info, read_envelope
from by2kb.providers.yt_dlp_source import _parse_subtitle, _select_subtitle, _utcnow
from by2kb.search.models import Candidate, clean_text


class SearchProvider(Protocol):
    name: str

    async def search(self, topic: str, limit: int, client: httpx.AsyncClient) -> list[Candidate]: ...

    async def preview(self, candidate: Candidate, languages: list[str], client: httpx.AsyncClient,
                      max_bytes: int) -> NormalizedTranscript | None: ...


# Isolated, killable worker: yt-dlp never downloads media or reads implicit CLI config.
# Emit only metadata/caption tracks; never persist media URLs, headers, or cookies.
_YTDLP_WORKER = r'''
import json, sys
import yt_dlp
sys.stdout.reconfigure(encoding='utf-8')
class Quiet:
    def debug(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass
source, flat = sys.argv[1], sys.argv[2] == '1'
options = dict(quiet=True, no_warnings=True, logger=Quiet(), skip_download=True,
               noplaylist=True, extract_flat=flat, socket_timeout=8, retries=0,
               extractor_retries=0, ignoreerrors=False, cachedir=False)
with yt_dlp.YoutubeDL(options) as ydl:
    info = ydl.extract_info(source, download=False)
def safe(item):
    keys = ('id', 'webpage_url', 'title', 'channel', 'uploader', 'duration', 'upload_date',
            'subtitles', 'automatic_captions', 'language', 'view_count', 'like_count')
    result = {k: item.get(k) for k in keys}
    result['description'] = str(item.get('description') or '')[:1500]
    return result
if flat:
    result = [safe(item) for item in (info.get('entries') or []) if item][:5]
else:
    if info.get('entries') is not None: raise ValueError('collection rejected')
    result = safe(info)
print(json.dumps(result, ensure_ascii=False))
'''


async def youtube_metadata(source: str, *, flat: bool = False) -> dict | list:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _YTDLP_WORKER, source, "1" if flat else "0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await process.communicate()
        if process.returncode:
            raise ConfigError("YouTube search/preview unavailable; check yt-dlp and network access")
        return json.loads(stdout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def caption_text(client: httpx.AsyncClient, url: str, max_bytes: int,
                       headers: dict | None = None) -> str:
    # Only platform subtitle CDNs; redirects must pass the same check.
    for _ in range(4):
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
                or not any(host == domain or host.endswith("." + domain)
                           for domain in ("youtube.com", "googlevideo.com", "hdslb.com", "bilibili.com"))):
            raise ValueError("unsupported caption endpoint")
        async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
            if response.is_redirect:
                url = str(response.url.join(response.headers["location"]))
                continue
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("caption preview exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8")
    raise ValueError("caption redirect limit exceeded")


def normalized_caption(candidate: Candidate, segments: list[Segment], *, language: str,
                       kind: str, provider: str) -> NormalizedTranscript:
    platform = "bilibili" if "bilibili.com" in candidate.url else "youtube"
    video_id = candidate.url.split("/video/")[1].strip("/") if platform == "bilibili" else candidate.url.split("v=")[1]
    return NormalizedTranscript(
        source=SourceMeta(platform=platform, video_id=video_id, canonical_url=candidate.url,
                          title=candidate.title, author=candidate.author,
                          duration_ms=int(candidate.duration_s * 1000) if candidate.duration_s else None),
        transcript=TranscriptMeta(provider=provider, kind=kind, language=language,
                                  fetched_at=_utcnow(), segments=segments),
    )


class YouTubeSearch:
    name = "youtube"

    async def search(self, topic, limit, client):
        del client
        entries = await youtube_metadata(f"ytsearch{limit}:{topic}", flat=True)
        results = []
        for entry in entries:
            try:
                results.append(Candidate(
                    provider=self.name, url=f"https://www.youtube.com/watch?v={entry['id']}",
                    title=entry.get("title") or entry["id"],
                    author=entry.get("channel") or entry.get("uploader") or "",
                    description=entry.get("description") or "", duration_s=entry.get("duration"),
                    published_at=entry.get("upload_date") or "",
                    view_count=entry.get("view_count"), like_count=entry.get("like_count"),
                ))
            except (KeyError, ValueError):
                continue
        return results

    async def preview(self, candidate, languages, client, max_bytes):
        info = await youtube_metadata(candidate.url)
        # Preserve metadata even when captions are unavailable or fail afterwards.
        for field in ("view_count", "like_count"):
            value = Candidate.model_validate({**candidate.model_dump(), field: info.get(field)})
            if getattr(value, field) is not None:
                setattr(candidate, field, getattr(value, field))
        selection = _select_subtitle(info, FetchOptions(preferred_languages=languages), "prefer")
        if selection is None:
            return None
        language, kind, track = selection
        text = await caption_text(client, str(track["url"]), max_bytes, track.get("http_headers"))
        segments = _parse_subtitle(text, str(track.get("ext") or ""))
        if not segments:
            raise ValueError("empty caption response")
        return normalized_caption(candidate, segments, language=language, kind=kind, provider="yt_dlp")


class BilibiliSearch:
    name = "bilibili"

    async def search(self, topic, limit, client):
        response = await client.get(
            f"{API_BASE}/x/web-interface/search/type",
            params={"search_type": "video", "keyword": topic, "page": 1},
            headers=_headers("https://www.bilibili.com/"),
        )
        response.raise_for_status()
        data = read_envelope(response.json(), what="video search")
        results = []
        for entry in (data.get("result") or [])[:limit]:
            try:
                duration = str(entry.get("duration") or "")
                parts = [float(p) for p in duration.split(":")] if duration else []
                seconds = sum(p * 60 ** i for i, p in enumerate(reversed(parts))) or None
                results.append(Candidate(
                    provider=self.name, url=f"https://www.bilibili.com/video/{entry['bvid']}/",
                    title=clean_text(entry.get("title")), author=entry.get("author") or "",
                    description=entry.get("description") or "", duration_s=seconds,
                    published_at=str(entry.get("pubdate") or ""),
                    view_count=entry.get("play", (entry.get("stat") or {}).get("view")),
                    like_count=entry.get("like", (entry.get("stat") or {}).get("like")),
                ))
            except (KeyError, ValueError):
                continue
        return results

    async def preview(self, candidate, languages, client, max_bytes):
        bvid = candidate.url.split("/video/")[1].strip("/")
        info = await fetch_video_info(client, bvid)
        if info.view_count is not None:
            candidate.view_count = info.view_count
        if info.like_count is not None:
            candidate.like_count = info.like_count
        response = await client.get(f"{API_BASE}/x/player/v2", params={"bvid": bvid, "cid": info.cid},
                                    headers=_headers(candidate.url))
        response.raise_for_status()
        data = read_envelope(response.json(), what="caption metadata")
        tracks = (data.get("subtitle") or {}).get("subtitles") or []
        # Logged-out absence says only "no accessible captions", never "no subtitles exist".
        tracks = sorted(tracks, key=lambda t: next(
            (i for i, language in enumerate(languages) if str(t.get("lan", "")).split("-")[0] == language.split("-")[0]),
            len(languages),
        ))
        if not tracks:
            return None
        track = tracks[0]
        url = str(track.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        text = await caption_text(client, url, max_bytes)
        segments = [Segment(start_ms=int(float(s["from"]) * 1000),
                            duration_ms=max(0, int((float(s["to"]) - float(s["from"])) * 1000)),
                            text=str(s["content"])) for s in json.loads(text).get("body", []) if s.get("content")]
        if not segments:
            raise ValueError("empty caption response")
        kind = "auto_caption" if str(track.get("lan", "")).startswith("ai-") else "human"
        return normalized_caption(candidate, segments, language=str(track.get("lan") or "zh"),
                                  kind=kind, provider="bilibili_caption")


class SearchProviderRegistry:
    def __init__(self):
        self._providers: dict[str, SearchProvider] = {}

    def register(self, provider: SearchProvider):
        if provider.name in self._providers:
            raise ConfigError(f"duplicate search provider: {provider.name}")
        self._providers[provider.name] = provider

    def enabled(self, config):
        if not config.enabled:
            raise ConfigError("topic search is disabled in [search]")
        unknown = set(config.providers) - set(self._providers)
        if unknown:
            raise ConfigError(f"unknown search provider: {sorted(unknown)[0]}")
        providers = [self._providers[name] for name in config.providers
                     if config.options.get(name, {}).get("enabled", True)]
        if not providers:
            raise ConfigError("no search providers enabled")
        return providers


def default_registry():
    registry = SearchProviderRegistry()
    registry.register(BilibiliSearch())
    registry.register(YouTubeSearch())
    return registry
