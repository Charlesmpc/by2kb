from __future__ import annotations

from dataclasses import dataclass

from by2kb.errors import ConfigError, UnsupportedUrl
from by2kb.providers.base import SourceProvider
from by2kb.providers.source_bilibili import BilibiliSourceProvider
from by2kb.providers.yt_dlp_source import YtDlpSourceConfig, YtDlpSourceProvider


@dataclass(frozen=True)
class SourceProviderRegistration:
    name: str
    provider: SourceProvider


class SourceProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, SourceProviderRegistration] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def register(self, name: str, provider: SourceProvider) -> None:
        normalized = name.strip().lower()
        if not normalized:
            raise ConfigError("source provider name cannot be empty")
        if normalized in self._providers:
            raise ConfigError(f"source provider already registered: {normalized}")
        if provider.name != normalized:
            raise ConfigError(
                f"source provider name mismatch: registered {normalized}, "
                f"provider reports {provider.name}"
            )
        self._providers[normalized] = SourceProviderRegistration(normalized, provider)

    def select(self, source: str, configured_order: list[str]) -> SourceProvider:
        if not configured_order:
            raise ConfigError("sources.providers cannot be empty")
        unknown = [
            name for name in configured_order if name.strip().lower() not in self._providers
        ]
        if unknown:
            available = ", ".join(self.names) or "none"
            raise ConfigError(
                f"unknown source provider: {unknown[0]}; available providers: {available}"
            )
        for requested in configured_order:
            provider = self._providers[requested.strip().lower()].provider
            if provider.supports(source):
                return provider
        raise UnsupportedUrl(f"no configured source provider supports: {source}")


def build_default_source_registry(
    *, source_options: dict[str, dict[str, object]] | None = None
) -> SourceProviderRegistry:
    options = source_options or {}
    registry = SourceProviderRegistry()
    registry.register("bilibili_native", BilibiliSourceProvider())
    registry.register(
        "yt_dlp",
        YtDlpSourceProvider(
            YtDlpSourceConfig.from_mapping(options.get("yt_dlp", {}))
        ),
    )
    return registry
