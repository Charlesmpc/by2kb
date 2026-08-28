from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from by2kb.errors import ConfigError, TerminalProviderError
from by2kb.providers.base import LocalAudio, SourceIdentity

AUDIO_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".wma"}
)
VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"}
)
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass(frozen=True)
class LocalMediaInfo:
    identity: SourceIdentity
    path: Path
    original_filename: str
    media_kind: str
    format: str
    size_bytes: int
    content_sha256: str

    def source_payload(self, *, duration_s: float | None) -> dict[str, object]:
        return {
            "local_media": {
                "original_filename": self.original_filename,
                "media_kind": self.media_kind,
                "format": self.format,
                "size_bytes": self.size_bytes,
                "content_sha256": self.content_sha256,
                "duration_s": duration_s,
            }
        }


def inspect_local_media(source: str | Path) -> LocalMediaInfo:
    supplied = Path(source).expanduser()
    try:
        path = supplied.resolve(strict=True)
    except FileNotFoundError as exc:
        raise TerminalProviderError(
            f"local media file not found: {supplied}", provider="local_media"
        ) from exc
    except OSError as exc:
        raise TerminalProviderError(
            f"cannot access local media file: {supplied}: {exc}",
            provider="local_media",
        ) from exc
    if not path.is_file():
        raise TerminalProviderError(
            f"local media path is not a file: {supplied}", provider="local_media"
        )
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise TerminalProviderError(
            f"unsupported local media type '{suffix or '(none)'}'; supported: {supported}",
            provider="local_media",
        )
    try:
        size = path.stat().st_size
        if size <= 0:
            raise TerminalProviderError(
                f"local media file is empty: {supplied}", provider="local_media"
            )
        digest = _sha256(path)
    except TerminalProviderError:
        raise
    except (OSError, PermissionError) as exc:
        raise TerminalProviderError(
            f"cannot read local media file: {supplied}: {exc}",
            provider="local_media",
        ) from exc
    identity = SourceIdentity(
        platform="local",
        video_id=digest,
        canonical_url=f"local://sha256/{digest}",
    )
    return LocalMediaInfo(
        identity=identity,
        path=path,
        original_filename=path.name,
        media_kind="audio" if suffix in AUDIO_EXTENSIONS else "video",
        format=suffix.lstrip("."),
        size_bytes=size,
        content_sha256=digest,
    )


async def prepare_local_audio(
    info: LocalMediaInfo,
    work_dir: Path,
) -> tuple[LocalAudio, float | None]:
    duration_s = await probe_duration(info.path)
    if info.media_kind == "audio":
        return (
            LocalAudio(
                path=info.path,
                format=info.format,
                duration_s=duration_s,
                size_bytes=info.size_bytes,
            ),
            duration_s,
        )

    output = work_dir / "local-audio.wav"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(info.path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(output),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ConfigError(
            "ffmpeg is required for local video files; install ffmpeg and run "
            "'by2kb doctor'"
        ) from exc
    _, stderr = await process.communicate()
    if process.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        detail = stderr.decode("utf-8", "replace").strip()
        raise TerminalProviderError(
            f"failed to extract audio from local video: {detail or 'ffmpeg produced no audio'}",
            provider="local_media",
        )
    return (
        LocalAudio(
            path=output,
            format="wav",
            duration_s=duration_s,
            size_bytes=output.stat().st_size,
        ),
        duration_s,
    )


async def probe_duration(path: Path) -> float | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ConfigError(
            "ffprobe is required for local media files; install ffmpeg and run "
            "'by2kb doctor'"
        ) from exc
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()
        raise TerminalProviderError(
            f"failed to inspect local media: {detail or 'ffprobe failed'}",
            provider="local_media",
        )
    try:
        value = json.loads(stdout.decode("utf-8"))["format"]["duration"]
        duration = float(value)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return duration if duration > 0 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
