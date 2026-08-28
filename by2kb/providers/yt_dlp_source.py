from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from by2kb.errors import (
    ConfigError,
    NeedsAuth,
    RateLimited,
    TerminalProviderError,
    TransientProviderError,
    UnsupportedUrl,
)
from by2kb.normalize import (
    NormalizedTranscript,
    Segment,
    TranscriptMeta,
    source_meta_from_identity,
)
from by2kb.providers.base import FetchOptions, LocalAudio, PreparedSource, SourceIdentity

_SAFE_PLATFORM = re.compile(r"[^a-z0-9_-]+")
_VTT_TIMESTAMP = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+"
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
)


@dataclass(frozen=True)
class YtDlpSourceConfig:
    enabled: bool = True
    subtitle_policy: str = "prefer"
    playlist_policy: str = "reject"
    cookie_file: Path | None = None
    cookies_from_browser: str | None = None

    @classmethod
    def from_mapping(cls, values: dict[str, object]) -> "YtDlpSourceConfig":
        policy = str(values.get("subtitle_policy", "prefer")).strip().lower()
        if policy not in {"prefer", "manual_only", "disabled"}:
            raise ConfigError(
                "yt-dlp subtitle_policy must be prefer, manual_only, or disabled"
            )
        playlist = str(values.get("playlist_policy", "reject")).strip().lower()
        if playlist != "reject":
            raise ConfigError("yt-dlp playlist_policy currently supports only reject")
        cookie_value = str(values.get("cookie_file") or "").strip()
        browser = str(values.get("cookies_from_browser") or "").strip()
        if cookie_value and browser:
            raise ConfigError(
                "configure only one of yt-dlp cookie_file or cookies_from_browser"
            )
        return cls(
            enabled=_as_bool(values.get("enabled", True)),
            subtitle_policy=policy,
            playlist_policy=playlist,
            cookie_file=Path(cookie_value).expanduser() if cookie_value else None,
            cookies_from_browser=browser or None,
        )


class YtDlpBackend:
    def _module(self):
        try:
            return importlib.import_module("yt_dlp")
        except ImportError as exc:
            raise ConfigError(
                "yt-dlp source provider is selected but yt-dlp is not installed; "
                "run: pipx inject by2kb 'yt-dlp>=2025.1.15'"
            ) from exc

    @property
    def version(self) -> str:
        module = self._module()
        return str(getattr(getattr(module, "version", None), "__version__", "unknown"))

    def extract(self, url: str, options: dict[str, object]) -> dict[str, Any]:
        module = self._module()
        runtime_options = dict(options)
        download = bool(runtime_options.pop("_download", False))
        try:
            with module.YoutubeDL(runtime_options) as ydl:
                result = ydl.extract_info(url, download=download)
        except Exception as exc:  # yt-dlp exception types are optional at import time
            raise _mapped_error(exc) from exc
        if not isinstance(result, dict):
            raise TerminalProviderError(
                "yt-dlp returned no source metadata", provider="yt_dlp"
            )
        return result


class YtDlpSourceProvider:
    name = "yt_dlp"

    def __init__(
        self,
        config: YtDlpSourceConfig,
        *,
        backend: YtDlpBackend | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or YtDlpBackend()
        self._info_by_id: dict[str, dict[str, Any]] = {}

    def supports(self, source: str) -> bool:
        candidate = (source or "").strip().lower()
        return self.config.enabled and candidate.startswith(("http://", "https://"))

    async def resolve(
        self, source: str, _client: httpx.AsyncClient
    ) -> SourceIdentity:
        if not self.supports(source):
            raise UnsupportedUrl(f"yt-dlp provider is disabled or unsupported: {source}")
        info = await asyncio.to_thread(
            self.backend.extract,
            source,
            self._options(download=False),
        )
        _reject_collection(info)
        video_id = str(info.get("id") or "").strip()
        if not video_id:
            raise TerminalProviderError(
                "yt-dlp metadata has no stable video id", provider=self.name
            )
        platform = _platform_name(info)
        canonical_url = str(
            info.get("webpage_url") or info.get("original_url") or source
        )
        self._info_by_id[video_id] = info
        return SourceIdentity(
            platform=platform,
            video_id=video_id,
            canonical_url=canonical_url,
        )

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
        info = self._info_by_id.pop(identity.video_id, None)
        if info is None:
            info = await asyncio.to_thread(
                self.backend.extract,
                identity.canonical_url,
                self._options(download=False),
            )
            _reject_collection(info)
        title = str(info.get("title") or identity.video_id)
        author = str(
            info.get("channel") or info.get("uploader") or info.get("creator") or ""
        )
        duration = _duration(info)
        provenance = self._provenance(info)
        cancel_check()

        if self.config.subtitle_policy != "disabled":
            set_stage("fetching_transcript")
            selection = _select_subtitle(info, options, self.config.subtitle_policy)
            if selection is not None:
                language, kind, track = selection
                try:
                    response = await client.get(
                        str(track["url"]),
                        headers={
                            str(k): str(v)
                            for k, v in (track.get("http_headers") or {}).items()
                        },
                    )
                    response.raise_for_status()
                    segments = _parse_subtitle(
                        response.text, str(track.get("ext") or "")
                    )
                    if segments:
                        normalized = NormalizedTranscript(
                            source=source_meta_from_identity(
                                identity,
                                title=title,
                                author=author,
                                duration_ms=(int(duration * 1000) if duration else None),
                            ),
                            transcript=TranscriptMeta(
                                provider=self.name,
                                model=str(info.get("extractor_key") or "yt-dlp"),
                                kind=kind,
                                language=language,
                                available_languages=_available_languages(info),
                                fetched_at=_utcnow(),
                                segments=segments,
                            ),
                        )
                        provenance.update(
                            {
                                "route": "subtitle",
                                "subtitle_language": language,
                                "subtitle_kind": kind,
                                "subtitle_format": track.get("ext"),
                            }
                        )
                        return PreparedSource(
                            title=title,
                            author=author,
                            duration_s=duration,
                            transcript=normalized,
                            source_payload={
                                "source_provider": self.name,
                                "provenance": provenance,
                            },
                        )
                except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                    provenance["subtitle_fallback"] = "caption download or parsing failed"
                cancel_check()

        if not options.allow_audio_fallback:
            raise TerminalProviderError(
                "no usable caption and audio fallback is disabled", provider=self.name
            )
        set_stage("capturing_media")
        work_dir.mkdir(parents=True, exist_ok=True)
        audio_info = await asyncio.to_thread(
            self.backend.extract,
            identity.canonical_url,
            self._options(download=True, work_dir=work_dir),
        )
        _reject_collection(audio_info)
        path = _downloaded_path(audio_info, work_dir)
        if not path.is_file():
            raise TerminalProviderError(
                "yt-dlp did not produce an audio file", provider=self.name
            )
        cancel_check()
        provenance["route"] = "audio_fallback"
        return PreparedSource(
            title=title,
            author=author,
            duration_s=duration,
            audio=LocalAudio(
                path=path,
                format=path.suffix.lstrip(".") or "unknown",
                duration_s=duration,
                size_bytes=path.stat().st_size,
            ),
            source_payload={"source_provider": self.name, "provenance": provenance},
        )

    def _options(
        self, *, download: bool, work_dir: Path | None = None
    ) -> dict[str, object]:
        options: dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": not download,
            "_download": download,
        }
        if download:
            if work_dir is None:  # pragma: no cover - internal contract guard
                raise ValueError("work_dir is required for a yt-dlp download")
            options.update(
                {
                    "format": "bestaudio/best",
                    "outtmpl": str(work_dir / "source.%(ext)s"),
                }
            )
        if self.config.cookie_file:
            options["cookiefile"] = str(self.config.cookie_file)
        if self.config.cookies_from_browser:
            options["cookiesfrombrowser"] = (self.config.cookies_from_browser,)
        return options

    def _provenance(self, info: dict[str, Any]) -> dict[str, object]:
        return {
            "provider": self.name,
            "provider_version": self.backend.version,
            "extractor": info.get("extractor"),
            "extractor_key": info.get("extractor_key"),
        }


def _select_subtitle(
    info: dict[str, Any], options: FetchOptions, policy: str
) -> tuple[str, str, dict[str, Any]] | None:
    groups: list[tuple[str, dict[str, Any]]] = [
        ("human", info.get("subtitles") or {})
    ]
    if policy == "prefer":
        groups.append(("auto_caption", info.get("automatic_captions") or {}))
    for kind, tracks in groups:
        for language in _language_order(tracks, options.preferred_languages):
            formats = tracks.get(language) or []
            selected = next(
                (
                    item
                    for ext in ("json3", "vtt")
                    for item in formats
                    if item.get("ext") == ext
                ),
                None,
            )
            if selected and selected.get("url"):
                return language, kind, selected
    return None


def _language_order(tracks: dict[str, Any], preferred: list[str]) -> list[str]:
    available = list(tracks)
    selected: list[str] = []
    for wanted in preferred:
        wanted_lower = wanted.lower()
        for language in available:
            language_lower = language.lower()
            if (
                language_lower == wanted_lower
                or language_lower.split("-")[0] == wanted_lower.split("-")[0]
            ):
                if language not in selected:
                    selected.append(language)
    return selected


def _available_languages(info: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *(info.get("subtitles") or {}).keys(),
                *(info.get("automatic_captions") or {}).keys(),
            ]
        )
    )


def _parse_subtitle(text: str, extension: str) -> list[Segment]:
    if extension == "json3":
        payload = json.loads(text)
        segments: list[Segment] = []
        for event in payload.get("events") or []:
            content = "".join(
                str(item.get("utf8") or "") for item in event.get("segs") or []
            ).strip()
            if content and content != "\n":
                segments.append(
                    Segment(
                        start_ms=int(event.get("tStartMs") or 0),
                        duration_ms=max(0, int(event.get("dDurationMs") or 0)),
                        text=" ".join(content.split()),
                    )
                )
        return segments
    return _parse_vtt(text)


def _parse_vtt(text: str) -> list[Segment]:
    lines = text.replace("\r\n", "\n").split("\n")
    segments: list[Segment] = []
    index = 0
    while index < len(lines):
        match = _VTT_TIMESTAMP.search(lines[index])
        if not match:
            index += 1
            continue
        start = _timestamp_ms(match.groups()[:4])
        end = _timestamp_ms(match.groups()[4:])
        index += 1
        content: list[str] = []
        while index < len(lines) and lines[index].strip():
            cleaned = re.sub(r"<[^>]+>", "", lines[index]).strip()
            if cleaned:
                content.append(cleaned)
            index += 1
        rendered = " ".join(content).strip()
        if rendered:
            segments.append(
                Segment(start_ms=start, duration_ms=max(0, end - start), text=rendered)
            )
    return segments


def _timestamp_ms(parts: tuple[str | None, ...]) -> int:
    hours, minutes, seconds, millis = parts
    return (
        int(hours or 0) * 3_600_000
        + int(minutes or 0) * 60_000
        + int(seconds or 0) * 1_000
        + int(millis or 0)
    )


def _downloaded_path(info: dict[str, Any], work_dir: Path) -> Path:
    candidates: list[Path] = []
    for item in info.get("requested_downloads") or []:
        value = item.get("filepath") or item.get("filename")
        if value:
            candidates.append(Path(str(value)))
    value = info.get("_filename")
    if value:
        candidates.append(Path(str(value)))
    candidates.extend(path for path in work_dir.glob("source.*") if path.is_file())
    return next((path for path in candidates if path.is_file()), work_dir / "missing")


def _reject_collection(info: dict[str, Any]) -> None:
    if (
        info.get("_type") in {"playlist", "multi_video"}
        or info.get("entries") is not None
    ):
        raise UnsupportedUrl(
            "playlist or multi-video URLs are rejected; submit one video URL"
        )


def _platform_name(info: dict[str, Any]) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "generic")
    normalized = _SAFE_PLATFORM.sub("_", extractor.lower()).strip("_")
    return normalized or "generic"


def _duration(info: dict[str, Any]) -> float | None:
    value = info.get("duration")
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _mapped_error(error: Exception):
    message = str(error)
    lowered = message.lower()
    if any(
        token in lowered for token in ("sign in", "login", "cookie", "private video")
    ):
        return NeedsAuth(f"yt-dlp authentication required: {message}", provider="yt_dlp")
    if "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        return RateLimited(f"yt-dlp rate limited: {message}", provider="yt_dlp")
    if any(
        token in lowered
        for token in ("timed out", "timeout", "temporary", "503", "502")
    ):
        return TransientProviderError(
            f"yt-dlp temporary failure: {message}", provider="yt_dlp"
        )
    return TerminalProviderError(f"yt-dlp extraction failed: {message}", provider="yt_dlp")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
