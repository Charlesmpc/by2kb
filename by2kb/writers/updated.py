from __future__ import annotations

from pathlib import Path

from by2kb.jobs.model import utcnow_iso
from by2kb.normalize import NormalizedTranscript
from by2kb.writers.raw import UPDATED_MD, render_frontmatter


def render_updated_md(
    normalized: NormalizedTranscript,
    *,
    body: str,
    skill_name: str,
    skill_version: str,
    model: str,
    provider: str,
) -> str:
    source = normalized.source
    frontmatter = render_frontmatter(
        {
            "schema_version": 1,
            "platform": source.platform,
            "video_id": source.video_id,
            "canonical_url": source.canonical_url,
            "title": source.title,
            "skills": f"{skill_name}@{skill_version}",
            "model": model,
            "provider": provider,
            "processed_at": utcnow_iso(),
            "raw_ref": "./raw.md",
            "confidence": "high" if normalized.transcript.kind != "asr" else "medium",
        }
    )
    return f"{frontmatter}\n\n{body.rstrip()}\n"


def write_updated_md(target_dir: Path, content: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / UPDATED_MD
    path.write_text(content, encoding="utf-8")
    return path
