from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from by2kb.config import DEFAULT_LLM_BASE_URL
from by2kb.errors import ConfigError
from by2kb.providers.asr_registry import build_default_asr_registry


@dataclass(frozen=True)
class InitSettings:
    library_root: Path
    source_providers: tuple[str, ...] = ("bilibili_native",)
    asr_provider: str = "faster_whisper"
    enrichment_executor: str = "external_agent"
    tos_access_key: str = ""
    tos_secret_key: str = ""
    tos_bucket: str = ""
    tos_region: str = "ap-southeast-1"
    tos_endpoint: str = ""
    doubao_api_key: str = ""
    doubao_app_id: str = ""
    doubao_access_token: str = ""
    doubao_resource_id: str = "volc.seedasr.auc"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"
    whisper_compute_type: str = "default"

    def validate(self) -> None:
        selected_asr = build_default_asr_registry().resolve_name(self.asr_provider)
        unknown_sources = set(self.source_providers) - {"bilibili_native", "yt_dlp"}
        if unknown_sources:
            raise ConfigError(
                "unsupported source provider: " + sorted(unknown_sources)[0]
            )
        if not self.source_providers:
            raise ConfigError("at least one source provider is required")
        missing: list[str] = []
        if selected_asr == "doubao_auc":
            required = {
                "VOLC_ACCESS_KEY_ID": self.tos_access_key,
                "VOLC_SECRET_ACCESS_KEY": self.tos_secret_key,
                "TOS_BUCKET": self.tos_bucket,
            }
            missing.extend(
                name for name, value in required.items() if not value.strip()
            )
            if not self.doubao_api_key.strip() and not (
                self.doubao_app_id.strip() and self.doubao_access_token.strip()
            ):
                missing.append(
                    "DOUBAO_API_KEY (or DOUBAO_APPID + DOUBAO_ACCESS_TOKEN)"
                )
        if self.enrichment_executor == "api" and not (
            self.llm_api_key.strip() and self.llm_model.strip()
        ):
            missing.append("BY2KB_LLM_API_KEY + BY2KB_LLM_MODEL")
        if self.enrichment_executor not in {"api", "external_agent", "disabled"}:
            raise ConfigError(
                f"unsupported enrichment executor: {self.enrichment_executor}"
            )
        if selected_asr == "faster_whisper":
            if not self.whisper_model.strip():
                missing.append("faster-whisper model")
            if self.whisper_device not in {"auto", "cpu", "cuda"}:
                raise ConfigError("faster-whisper device must be auto, cpu, or cuda")
            if not self.whisper_compute_type.strip():
                missing.append("faster-whisper compute type")
        values = (
            self.tos_access_key,
            self.tos_secret_key,
            self.tos_bucket,
            self.tos_region,
            self.tos_endpoint,
            self.doubao_api_key,
            self.doubao_app_id,
            self.doubao_access_token,
            self.doubao_resource_id,
            self.llm_api_key,
            self.llm_model,
            self.llm_base_url,
            self.whisper_model,
            self.whisper_device,
            self.whisper_compute_type,
        )
        if any("\n" in value or "\r" in value for value in values):
            raise ConfigError("configuration values must not contain newlines")
        if missing:
            raise ConfigError("missing required configuration: " + ", ".join(missing))


def render_config_toml(settings: InitSettings) -> str:
    lines = [
        f"library_root = {json.dumps(str(settings.library_root), ensure_ascii=False)}",
        'destination = "filesystem:library"',
        'preferred_languages = ["zh-CN", "zh", "en"]',
        "",
        "[sources]",
        "providers = " + json.dumps(list(settings.source_providers)),
        "",
        "[asr]",
        f"provider = {json.dumps(settings.asr_provider)}",
    ]
    if settings.asr_provider.strip().lower() == "faster_whisper":
        lines.extend(
            [
                f"model = {json.dumps(settings.whisper_model)}",
                f"device = {json.dumps(settings.whisper_device)}",
                f"compute_type = {json.dumps(settings.whisper_compute_type)}",
            ]
        )
    if "yt_dlp" in settings.source_providers:
        lines.extend(
            [
                "",
                "[sources.yt_dlp]",
                'subtitle_policy = "prefer"',
                'playlist_policy = "reject"',
            ]
        )
    lines.extend(
        [
            "",
            "[enrichment]",
            f"executor = {json.dumps(settings.enrichment_executor)}",
            'abstract_profile = "short-video-abstract"',
            'study_profile = "default-video-digest"',
        ]
    )
    if settings.enrichment_executor == "api":
        lines.extend(
            [
                "",
                "[llm]",
                f"base_url = {json.dumps(settings.llm_base_url)}",
                f"model = {json.dumps(settings.llm_model)}",
            ]
        )
    return "\n".join(lines) + "\n"


def render_env(settings: InitSettings) -> str:
    values: dict[str, str] = {}
    if settings.asr_provider.strip().lower() in {"doubao_auc", "auto"}:
        endpoint = settings.tos_endpoint.strip() or (
            f"tos-s3-{settings.tos_region}.volces.com"
        )
        values.update(
            {
                "VOLC_ACCESS_KEY_ID": settings.tos_access_key,
                "VOLC_SECRET_ACCESS_KEY": settings.tos_secret_key,
                "TOS_BUCKET": settings.tos_bucket,
                "TOS_REGION": settings.tos_region,
                "TOS_S3_ENDPOINT": endpoint,
                "DOUBAO_API_KEY": settings.doubao_api_key,
                "DOUBAO_APPID": settings.doubao_app_id,
                "DOUBAO_ACCESS_TOKEN": settings.doubao_access_token,
                "DOUBAO_RESOURCE_ID": settings.doubao_resource_id,
            }
        )
    if settings.enrichment_executor == "api":
        values.update(
            {
                "BY2KB_LLM_API_KEY": settings.llm_api_key,
                "BY2KB_LLM_MODEL": settings.llm_model,
                "BY2KB_LLM_BASE_URL": settings.llm_base_url,
            }
        )
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def write_initial_config(
    home: Path,
    settings: InitSettings,
    *,
    force: bool = False,
) -> tuple[Path, Path]:
    settings.validate()
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"
    env_path = home / ".env"
    existing = [path for path in (config_path, env_path) if path.exists()]
    if existing and not force:
        raise ConfigError(
            "configuration already exists: " + ", ".join(str(path) for path in existing)
        )
    try:
        settings.library_root.expanduser().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(
            f"cannot create knowledge-base folder: {settings.library_root}: {exc}"
        ) from exc
    config_path.write_text(render_config_toml(settings), encoding="utf-8")
    env_path.write_text(render_env(settings), encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    return config_path, env_path
