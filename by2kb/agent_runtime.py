from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from by2kb.errors import ConfigError
from by2kb.operation_lock import exclusive_file


class OperationConflict(ConfigError):
    def __init__(self, reason: str, submitted_id: str, expected_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.submitted_id = submitted_id
        self.expected_id = expected_id


SESSION_SCHEMA_VERSION = 1
MAX_AGENT_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_OPERATION_TIMEOUT_S = 600


@dataclass(frozen=True)
class AgentOperation:
    id: str
    system_prompt: str
    user_prompt: str
    max_output_bytes: int = MAX_AGENT_OUTPUT_BYTES
    timeout_s: int = DEFAULT_OPERATION_TIMEOUT_S

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "max_output_bytes": self.max_output_bytes,
            "timeout_s": self.timeout_s,
        }


class AgentOperationRequired(Exception):
    def __init__(self, operation: AgentOperation):
        super().__init__(f"Agent operation required: {operation.id}")
        self.operation = operation


class AgentSessionStore:
    def __init__(
        self,
        root: Path,
        *,
        provider: str,
        model: str,
        runtime_version: str,
    ):
        self.provider = _required_identity(provider, "provider")
        self.model = _required_identity(model, "model")
        self.runtime_version = _optional_identity(runtime_version, "runtime version")
        identity = json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "runtime_version": self.runtime_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        self.path = root / f"session-{identity_hash}.json"

    def response(self, operation_id: str) -> str | None:
        payload = self._read()
        value = payload["responses"].get(operation_id)
        return value if isinstance(value, str) and value.strip() else None

    def pending(self) -> AgentOperation | None:
        value = self._read().get("pending")
        if not isinstance(value, dict):
            return None
        try:
            return AgentOperation(
                id=str(value["id"]),
                system_prompt=str(value["system_prompt"]),
                user_prompt=str(value["user_prompt"]),
                max_output_bytes=int(value["max_output_bytes"]),
                timeout_s=int(value["timeout_s"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def set_pending(self, operation: AgentOperation) -> None:
        with exclusive_file(self.path.with_suffix(".lock")):
            payload = self._read()
            payload["pending"] = operation.to_dict()
            self._write(payload)

    def submit(self, operation_id: str, content: str) -> str:
        with exclusive_file(self.path.with_suffix(".lock")):
            return self._submit_locked(operation_id, content)

    def _submit_locked(self, operation_id: str, content: str) -> str:
        payload = self._read()
        pending = payload.get("pending")
        if operation_id in payload["responses"]:
            if payload["responses"][operation_id] == content:
                return "already_accepted"
            raise OperationConflict("operation_content_conflict", operation_id)
        expected_id = pending.get("id") if isinstance(pending, dict) else None
        if expected_id is None or operation_id != expected_id:
            raise OperationConflict("operation_mismatch", operation_id, expected_id)
        encoded = content.encode("utf-8")
        if not content.strip():
            raise ConfigError("Agent enrichment output must not be empty")
        if len(encoded) > pending["max_output_bytes"]:
            raise ConfigError(
                f"Agent enrichment output exceeds {pending['max_output_bytes']} bytes"
            )
        payload["responses"][operation_id] = content
        payload["pending"] = None
        self._write(payload)
        return "accepted"

    def _read(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {}
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigError("Agent enrichment session is unreadable") from exc
        if not payload:
            return {
                "schema_version": SESSION_SCHEMA_VERSION,
                "provider": self.provider,
                "model": self.model,
                "runtime_version": self.runtime_version,
                "pending": None,
                "responses": {},
            }
        if payload.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise ConfigError("unsupported Agent enrichment session schema")
        if (
            payload.get("provider") != self.provider
            or payload.get("model") != self.model
            or payload.get("runtime_version") != self.runtime_version
        ):
            raise ConfigError("Agent enrichment session identity mismatch")
        if not isinstance(payload.get("responses"), dict):
            raise ConfigError("Agent enrichment session responses are invalid")
        return payload

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


class AgentCallbackClient:
    def __init__(self, session: AgentSessionStore):
        self._session = session
        self.provider = session.provider
        self.model = session.model
        self.runtime_version = session.runtime_version

    async def complete(self, system: str, user: str) -> str:
        operation_id = _operation_id(
            provider=self.provider,
            model=self.model,
            runtime_version=self.runtime_version,
            system=system,
            user=user,
        )
        cached = self._session.response(operation_id)
        if cached is not None:
            return cached
        operation = AgentOperation(
            id=operation_id,
            system_prompt=system,
            user_prompt=user,
        )
        self._session.set_pending(operation)
        raise AgentOperationRequired(operation)


def _operation_id(
    *,
    provider: str,
    model: str,
    runtime_version: str,
    system: str,
    user: str,
) -> str:
    encoded = json.dumps(
        {
            "schema_version": SESSION_SCHEMA_VERSION,
            "provider": provider,
            "model": model,
            "runtime_version": runtime_version,
            "system_prompt": system,
            "user_prompt": user,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_identity(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ConfigError(f"Agent runtime {label} must not be empty")
    if any(character in normalized for character in "\r\n"):
        raise ConfigError(f"Agent runtime {label} must not contain newlines")
    return normalized


def _optional_identity(value: str, label: str) -> str:
    normalized = value.strip()
    if any(character in normalized for character in "\r\n"):
        raise ConfigError(f"Agent runtime {label} must not contain newlines")
    return normalized
