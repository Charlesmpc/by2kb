from __future__ import annotations

import re

MAX_TITLE_CHARS = 80

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_title(title: str) -> str:
    text = _UNSAFE_CHARS.sub(" ", str(title or ""))
    text = " ".join(text.split())
    text = text.strip(" .")
    if not text:
        return "untitled"
    stem = text.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        text = f"_{text}"
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rstrip(" .")
    return text or "untitled"


def artifact_basename(title: str, video_id: str) -> str:
    return f"{sanitize_title(title)}-{video_id}"


def markdown_artifact_name(title: str, video_id: str, kind: str) -> str:
    if kind not in ("raw", "updated"):
        raise ValueError(f"unknown markdown artifact kind: {kind}")
    return f"{artifact_basename(title, video_id)}.{kind}.md"
