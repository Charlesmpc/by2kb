from __future__ import annotations

import html
import math
import re
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, field_validator

from by2kb.errors import By2kbError
from by2kb.providers.bilibili import resolve


def clean_text(value: object, limit: int = 1500) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())[:limit]


def canonical_video_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("candidate must be a public video URL")
    if parsed.hostname in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        try:
            return resolve(url).canonical_url
        except By2kbError as exc:
            raise ValueError("candidate must be a single Bilibili video") from exc
    video_id = ""
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.strip("/")
    elif parsed.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith("/shorts/"):
            video_id = parsed.path.split("/")[2]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("candidate must be one Bilibili or YouTube video")
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_count(value: object) -> int | None:
    """Keep missing/private metrics distinct from a genuine zero."""
    if value is None or isinstance(value, bool):
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万亿]?)", str(value).replace(",", "").strip())
    if not match:
        return None
    number = float(match[1]) * {"": 1, "万": 10000, "亿": 100000000}[match[2]]
    return int(number) if math.isfinite(number) and number.is_integer() else None


class Candidate(BaseModel):
    provider: str
    url: str
    title: str
    author: str = ""
    duration_s: float | None = None
    view_count: int | None = None
    like_count: int | None = None
    description: str = ""
    published_at: str = ""
    content_type: Literal["video"] = "video"
    preview_status: Literal["metadata_only", "captions_ready", "no_accessible_captions", "preview_failed"] = "metadata_only"
    preview_excerpt: str = ""
    caption_sha256: str | None = None
    reason: str = ""

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return canonical_video_url(value)

    @field_validator("title", "author", "description", "published_at", "reason", "preview_excerpt", mode="before")
    @classmethod
    def safe_text(cls, value):
        return clean_text(value)

    @field_validator("view_count", "like_count", mode="before")
    @classmethod
    def safe_count(cls, value):
        return parse_count(value)

    @field_validator("duration_s")
    @classmethod
    def finite_duration(cls, value):
        if value is not None and (not 0 < value < 10_000_000):
            return None
        return value
