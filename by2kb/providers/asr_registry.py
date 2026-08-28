from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import httpx

from by2kb.errors import ConfigError
from by2kb.providers.asr import AsrProvider

AsrProviderFactory = Callable[[httpx.AsyncClient], AsrProvider]


@dataclass(frozen=True)
class AsrProviderRegistration:
    name: str
    factory: AsrProviderFactory
    priority: int = 0


class AsrProviderRegistry:
    """Resolve configured ASR names without coupling the job runner to providers."""

    def __init__(self) -> None:
        self._registrations: dict[str, AsrProviderRegistration] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(registration.name for registration in self._ordered())

    def register(
        self,
        name: str,
        factory: AsrProviderFactory,
        *,
        priority: int = 0,
        replace: bool = False,
    ) -> None:
        normalized = _normalize_name(name)
        if normalized == "auto":
            raise ConfigError("'auto' is reserved for ASR provider selection")
        if normalized in self._registrations and not replace:
            raise ConfigError(f"ASR provider already registered: {normalized}")
        self._registrations[normalized] = AsrProviderRegistration(
            name=normalized,
            factory=factory,
            priority=priority,
        )

    def resolve_name(self, configured_name: str) -> str:
        normalized = _normalize_name(configured_name)
        if normalized == "auto":
            ordered = self._ordered()
            if not ordered:
                raise ConfigError("no ASR providers are registered")
            return ordered[0].name
        if normalized not in self._registrations:
            available = ", ".join(self.names) or "none"
            raise ConfigError(
                f"unknown ASR provider: {configured_name}; available providers: {available}"
            )
        return normalized

    def create(
        self, configured_name: str, client: httpx.AsyncClient
    ) -> AsrProvider:
        normalized = _normalize_name(configured_name)
        registrations = (
            self._ordered()
            if normalized == "auto"
            else [self._registrations[self.resolve_name(normalized)]]
        )
        failures: list[str] = []
        for registration in registrations:
            try:
                provider = registration.factory(client)
            except ConfigError as exc:
                if normalized != "auto":
                    raise ConfigError(
                        f"ASR provider '{registration.name}' is unavailable: {exc}"
                    ) from exc
                failures.append(f"{registration.name}: {exc}")
                continue
            if not isinstance(provider, AsrProvider):
                raise ConfigError(
                    f"ASR provider factory '{registration.name}' returned an invalid provider"
                )
            if provider.name != registration.name:
                raise ConfigError(
                    "ASR provider factory name mismatch: "
                    f"registered '{registration.name}', returned '{provider.name}'"
                )
            return provider
        detail = "; ".join(failures) or "no providers are registered"
        raise ConfigError(f"no configured ASR provider is available: {detail}")

    def _ordered(self) -> list[AsrProviderRegistration]:
        return sorted(
            self._registrations.values(),
            key=lambda registration: (-registration.priority, registration.name),
        )


def build_default_asr_registry(
    *,
    asr_options: Mapping[str, object] | None = None,
    home: Path | None = None,
) -> AsrProviderRegistry:
    registry = AsrProviderRegistry()
    registry.register(
        "faster_whisper",
        lambda client: _create_faster_whisper(client, asr_options, home),
        priority=200,
    )
    registry.register("doubao_auc", _create_doubao_auc, priority=100)
    return registry


def _create_faster_whisper(
    _client: httpx.AsyncClient,
    options: Mapping[str, object] | None,
    home: Path | None,
) -> AsrProvider:
    from by2kb.providers.asr_faster_whisper import (
        FasterWhisperAsrProvider,
        FasterWhisperConfig,
        require_faster_whisper_ready,
    )

    config = FasterWhisperConfig.from_mapping(options, home=home)
    require_faster_whisper_ready(config)
    return FasterWhisperAsrProvider(config)


def _create_doubao_auc(client: httpx.AsyncClient) -> AsrProvider:
    from importlib.util import find_spec

    from by2kb.providers.asr_doubao_auc import (
        DoubaoAucAsrProvider,
        DoubaoAucConfig,
    )

    if find_spec("boto3") is None:
        raise ConfigError("doubao_auc requires boto3: pip install by2kb[asr-doubao]")
    config = DoubaoAucConfig.from_env()
    if not config.api_key and not (config.app_id and config.access_token):
        raise ConfigError(
            "missing required environment variable: DOUBAO_API_KEY "
            "(or legacy DOUBAO_APPID + DOUBAO_ACCESS_TOKEN)"
        )
    return DoubaoAucAsrProvider(config, client)


def _normalize_name(name: str) -> str:
    normalized = (name or "").strip().lower()
    if not normalized:
        raise ConfigError("ASR provider name must not be empty")
    return normalized
