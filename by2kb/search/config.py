from __future__ import annotations

from dataclasses import dataclass, field

from by2kb.errors import ConfigError


@dataclass(frozen=True)
class SearchConfig:
    enabled: bool = True
    providers: tuple[str, ...] = ("bilibili", "youtube")
    max_candidates: int = 3
    timeout_s: float = 20
    total_timeout_s: float = 28
    attempt_timeout_s: float = 8
    max_attempts: int = 3
    retry_base_s: float = 2
    preview_mode: str = "captions_only"
    preview_timeout_s: float = 20
    preview_max_bytes: int = 2_000_000
    session_ttl_s: int = 3600
    options: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, values: dict) -> "SearchConfig":
        if not isinstance(values, dict):
            raise ConfigError("search must be a table")
        preview = values.get("preview", {})
        if not isinstance(preview, dict):
            raise ConfigError("search.preview must be a table")
        enabled = values.get("enabled", True)
        providers = values.get("providers", ["bilibili", "youtube"])
        if not isinstance(enabled, bool) or not isinstance(providers, list) or any(
            not isinstance(p, str) or not p.strip() for p in providers
        ) or len(set(providers)) != len(providers):
            raise ConfigError("search.enabled must be boolean and providers a unique name list")
        options = {k: dict(v) for k, v in values.items() if k != "preview" and isinstance(v, dict)}
        for name, option in options.items():
            if not isinstance(option.get("enabled", True), bool):
                raise ConfigError(f"search.{name}.enabled must be boolean")
        try:
            config = cls(
                enabled=enabled, providers=tuple(providers),
                max_candidates=int(values.get("max_candidates", 3)),
                timeout_s=float(values.get("timeout_s", 20)),
                total_timeout_s=float(values.get("total_timeout_s", 28)),
                attempt_timeout_s=float(values.get("attempt_timeout_s", 8)),
                max_attempts=int(values.get("max_attempts", 3)),
                retry_base_s=float(values.get("retry_base_s", 2)),
                preview_mode=str(preview.get("mode", "captions_only")),
                preview_timeout_s=float(preview.get("timeout_s", 20)),
                preview_max_bytes=int(preview.get("max_bytes", 2_000_000)),
                session_ttl_s=int(values.get("session_ttl_s", 3600)), options=options,
            )
        except (ValueError, TypeError, OverflowError) as exc:
            raise ConfigError("invalid search numeric setting") from exc
        if not 3 <= config.max_candidates <= 5:
            raise ConfigError("search.max_candidates must be 3..5")
        if not 1 <= config.timeout_s <= 120 or not 1 <= config.preview_timeout_s <= 120:
            raise ConfigError("search and preview timeouts must be 1..120 seconds")
        import math
        if (not all(math.isfinite(v) for v in (config.timeout_s, config.preview_timeout_s, config.total_timeout_s, config.attempt_timeout_s, config.retry_base_s))
                or not 5 <= config.total_timeout_s <= 120 or not 1 <= config.attempt_timeout_s <= 30
                or not 1 <= config.max_attempts <= 3 or not 0 <= config.retry_base_s <= 10):
            raise ConfigError("invalid search retry or overall timeout setting")
        if config.preview_mode not in {"metadata_only", "captions_only"}:
            raise ConfigError("search.preview.mode must be metadata_only or captions_only")
        if not 1024 <= config.preview_max_bytes <= 10_000_000 or not 60 <= config.session_ttl_s <= 86400:
            raise ConfigError("preview max_bytes must be 1024..10000000 and session_ttl_s 60..86400")
        return config
