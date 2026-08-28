from __future__ import annotations

import hashlib
import json
from pathlib import Path

from by2kb.filenames import markdown_artifact_name
from by2kb.normalize import NormalizedTranscript, format_timestamp

SOURCE_JSON = "source.json"
TRANSCRIPT_JSON = "transcript.json"

KIND_SOURCE_JSON = "source_json"
KIND_TRANSCRIPT_JSON = "transcript_json"
KIND_RAW_MD = "raw_md"


def _yaml_escape(value: object) -> str:
    text = str(value if value is not None else "")
    if text == "" or any(ch in text for ch in ":#\"'") or text != text.strip():
        return json.dumps(text, ensure_ascii=False)
    return text


def render_frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {_yaml_escape(value)}")
    lines.append("---")
    return "\n".join(lines)


def render_raw_md(normalized: NormalizedTranscript) -> str:
    source = normalized.source
    transcript = normalized.transcript
    frontmatter = render_frontmatter(
        {
            "schema_version": normalized.schema_version,
            "platform": source.platform,
            "video_id": source.video_id,
            "canonical_url": source.canonical_url,
            "title": source.title,
            "author": source.author,
            "duration_ms": source.duration_ms if source.duration_ms is not None else "",
            "language": transcript.language or "",
            "transcript_provider": transcript.provider,
            "transcript_model": transcript.model or "",
            "transcript_kind": transcript.kind,
            "fetched_at": transcript.fetched_at,
        }
    )
    lines = [frontmatter, "", f"# {source.title}", ""]
    if transcript.kind == "asr" and len(transcript.segments) <= 1:
        lines.append(
            "> ASR output without per-segment timing; text covers the full audio."
        )
        lines.append("")
        if transcript.segments:
            lines.append(transcript.segments[0].text)
    else:
        for segment in transcript.segments:
            lines.append(f"[{format_timestamp(segment.start_ms)}] {segment.text}")
    return "\n".join(lines).rstrip() + "\n"


def write_artifacts(
    target_dir: Path,
    *,
    source_payload: dict,
    normalized: NormalizedTranscript,
) -> dict[str, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_md = render_raw_md(normalized)
    raw_name = markdown_artifact_name(
        normalized.source.title, normalized.source.video_id, "raw"
    )
    outputs = {
        KIND_SOURCE_JSON: target_dir / SOURCE_JSON,
        KIND_TRANSCRIPT_JSON: target_dir / TRANSCRIPT_JSON,
        KIND_RAW_MD: target_dir / raw_name,
    }
    outputs[KIND_SOURCE_JSON].write_text(
        json.dumps(source_payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    outputs[KIND_TRANSCRIPT_JSON].write_text(
        normalized.model_dump_json(indent=1) + "\n", encoding="utf-8"
    )
    outputs[KIND_RAW_MD].write_text(raw_md, encoding="utf-8")
    return outputs


def content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
