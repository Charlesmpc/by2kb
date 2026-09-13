"""Small control envelopes and lossless, paged Agent request files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from by2kb.errors import ConfigError
from by2kb.longform import estimate_tokens

MAX_REQUEST_BYTES = 32 * 1024 * 1024


def export_request(root: Path, payload: dict) -> dict:
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ConfigError("Agent request exceeds 32 MiB; reduce the configured input budget")
    digest = hashlib.sha256(encoded).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"request-{digest}.json"
    # Content-addressed immutable files: concurrent exports have identical bytes.
    import tempfile
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as output:
        temporary = Path(output.name)
        output.write(encoded)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"request_file": str(path.resolve()), "request_sha256": digest,
            "request_bytes": len(encoded)}


def operation_ticket(root: Path, operation: dict) -> dict:
    return {
        "id": operation["id"],
        "max_output_bytes": operation["max_output_bytes"],
        "timeout_s": operation["timeout_s"],
        "estimated_input_tokens": estimate_tokens(operation["system_prompt"] + operation["user_prompt"]),
        "prompt_chars": {field: len(operation[field]) for field in ("system_prompt", "user_prompt")},
        **export_request(root, operation),
    }


def read_request_page(path: Path, field: str, offset: int, limit: int) -> dict:
    if field not in {"system_prompt", "user_prompt"}:
        raise ConfigError("field must be system_prompt or user_prompt")
    if offset < 0 or not 1 <= limit <= 2000:
        raise ConfigError("offset must be nonnegative; limit must be 1..2000 characters")
    try:
        with path.open("rb") as stream:
            encoded = stream.read(MAX_REQUEST_BYTES + 1)
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ConfigError("Agent request exceeds 32 MiB")
        digest = hashlib.sha256(encoded).hexdigest()
        if path.name != f"request-{digest}.json":
            raise ConfigError("Agent request checksum mismatch; query next again")
        payload = json.loads(encoded)
        content = payload[field]
        if not isinstance(content, str) or not isinstance(payload.get("id"), str):
            raise ValueError("invalid operation")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError("Agent request must be a readable operation file") from exc
    if offset > len(content):
        raise ConfigError("offset is beyond the end of the prompt")
    end = min(offset + limit, len(content))
    return {"operation_id": payload["id"], "field": field, "offset": offset,
            "next_offset": end, "total_chars": len(content), "eof": end == len(content),
            "text": content[offset:end], "request_sha256": digest}
