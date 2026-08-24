from __future__ import annotations

import re
import unicodedata

MAX_TITLE_CHARS = 80

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_title(title: str) -> str:
    text = "".join(
        " " if unicodedata.category(char) == "Cc" else char
        for char in str(title or "")
    )
    text = _UNSAFE_CHARS.sub(" ", text)
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
    prefixes = {
        "raw": "raw",
        "abstract": "short",
        "updated": "long",
    }
    if kind not in prefixes:
        raise ValueError(f"unknown markdown artifact kind: {kind}")
    del video_id  # The parent directory is the durable video identity.
    return f"{prefixes[kind]}.{sanitize_title(title)}.md"
