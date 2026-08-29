from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlparse

from by2kb.config import Config
from by2kb.errors import ConfigError
from by2kb.providers.asr_faster_whisper import (
    FasterWhisperConfig,
    faster_whisper_status,
)
from by2kb.providers.asr_registry import build_default_asr_registry
from by2kb.providers.yt_dlp_source import YtDlpSourceConfig

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    ok: bool
    message: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DoctorReport:
    provider: str
    enrichment_executor: str
    source_providers: tuple[str, ...]
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": self.ok,
            "provider": self.provider,
            "source_providers": list(self.source_providers),
            "enrichment_executor": self.enrichment_executor,
            "checks": [check.to_dict() for check in self.checks],
        }


def run_doctor(config: Config, *, provider: str | None = None) -> DoctorReport:
    checks = [
        _command_check("ffmpeg", "ffmpeg"),
        _command_check("ffprobe", "ffprobe"),
        _directory_check(
            "library_writable",
            config.library_root,
            "Knowledge-library destination",
        ),
        _directory_check("home_writable", config.home, "by2kb home directory"),
        _database_check(config.db_path),
    ]
    checks.extend(_source_checks(config))

    requested = (provider or config.asr_provider).strip().lower()
    selected = _select_provider_for_diagnostics(config, requested)
    checks.extend(_asr_checks(config, selected))
    checks.extend(_enrichment_checks(config))
    return DoctorReport(
        provider=selected,
        enrichment_executor=config.resolved_enrichment_executor(),
        source_providers=tuple(config.sources.providers),
        checks=tuple(checks),
    )


def _source_checks(config: Config) -> list[DoctorCheck]:
    supported = {"bilibili_native", "yt_dlp"}
    checks: list[DoctorCheck] = []
    for name in config.sources.providers:
        normalized = name.strip().lower()
        if normalized not in supported:
            checks.append(
                DoctorCheck(
                    f"source_{normalized or 'empty'}",
                    False,
                    f"Unknown source provider: {name}",
                    "Use bilibili_native or yt_dlp in [sources].providers.",
                )
            )
            continue
        if normalized == "bilibili_native":
            checks.append(
                DoctorCheck(
                    "source_bilibili_native",
                    True,
                    "Native Bilibili source provider is available",
                )
            )
            continue
        try:
            source_config = YtDlpSourceConfig.from_mapping(
                config.sources.options.get("yt_dlp", {})
            )
        except ConfigError as exc:
            checks.append(
                DoctorCheck(
                    "source_yt_dlp_config",
                    False,
                    "yt-dlp source configuration is invalid",
                    str(exc),
                )
            )
            continue
        installed = find_spec("yt_dlp") is not None
        checks.append(
            DoctorCheck(
                "source_yt_dlp_dependency",
                installed,
                "yt-dlp dependency is installed" if installed else "yt-dlp dependency is missing",
                None
                if installed
                else "Run: pipx inject by2kb 'yt-dlp>=2025.1.15'",
            )
        )
        if source_config.cookie_file:
            readable = source_config.cookie_file.is_file() and os.access(
                source_config.cookie_file, os.R_OK
            )
            checks.append(
                DoctorCheck(
                    "source_yt_dlp_cookies",
                    readable,
                    "yt-dlp cookie file is readable" if readable else "yt-dlp cookie file is not readable",
                    None if readable else "Correct or remove [sources.yt_dlp].cookie_file.",
                )
            )
    return checks


def _select_provider_for_diagnostics(config: Config, requested: str) -> str:
    registry = build_default_asr_registry(
        asr_options=config.asr_options,
        home=config.home,
    )
    if requested != "auto":
        return registry.resolve_name(requested)
    whisper = FasterWhisperConfig.from_mapping(
        config.asr_options,
        home=config.home,
    )
    status = faster_whisper_status(whisper)
    if status["dependency_installed"] and status["model_installed"]:
        return "faster_whisper"
    return "doubao_auc"


def _command_check(check_id: str, command: str) -> DoctorCheck:
    executable = shutil.which(command)
    remediation = "Install ffmpeg and ensure ffmpeg and ffprobe are on PATH."
    if not executable:
        return DoctorCheck(check_id, False, f"{command} was not found on PATH", remediation)
    try:
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return DoctorCheck(check_id, False, f"{command} could not execute", remediation)
    if completed.returncode != 0:
        return DoctorCheck(
            check_id,
            False,
            f"{command} exited with status {completed.returncode}",
            remediation,
        )
    return DoctorCheck(check_id, True, f"{command} is available")


def _directory_check(check_id: str, path: Path, label: str) -> DoctorCheck:
    candidate = path.expanduser()
    ok = candidate.is_dir() and os.access(candidate, os.W_OK)
    if ok:
        return DoctorCheck(check_id, True, f"{label} is writable")
    return DoctorCheck(
        check_id,
        False,
        f"{label} is not writable",
        f"Create the directory or grant write permission: {candidate}",
    )


def _database_check(path: Path) -> DoctorCheck:
    candidate = path.expanduser()
    if not candidate.exists():
        return _directory_check(
            "database_writable", candidate.parent, "job database directory"
        )
    ok = candidate.is_file() and os.access(candidate, os.R_OK | os.W_OK)
    return DoctorCheck(
        "database_writable",
        ok,
        "Job database is readable and writable" if ok else "Job database is not writable",
        None if ok else f"Grant read and write permission: {candidate}",
    )


def _asr_checks(config: Config, provider: str) -> list[DoctorCheck]:
    if provider == "faster_whisper":
        whisper = FasterWhisperConfig.from_mapping(
            config.asr_options,
            home=config.home,
        )
        status = faster_whisper_status(whisper)
        return [
            DoctorCheck(
                "asr_dependency",
                bool(status["dependency_installed"]),
                (
                    "faster-whisper dependency is installed"
                    if status["dependency_installed"]
                    else "faster-whisper dependency is missing"
                ),
                None
                if status["dependency_installed"]
                else "Run: pipx inject by2kb 'faster-whisper>=1.2.1,<2'",
            ),
            DoctorCheck(
                "asr_model",
                bool(status["model_installed"]),
                (
                    f"faster-whisper model {status['model']} is installed"
                    if status["model_installed"]
                    else f"faster-whisper model {status['model']} is missing"
                ),
                None
                if status["model_installed"]
                else f"Run: by2kb models install {status['model']}",
            ),
        ]
    return _doubao_checks()


def _doubao_checks() -> list[DoctorCheck]:
    dependency_ok = find_spec("boto3") is not None
    required = {
        "VOLC_ACCESS_KEY_ID": os.environ.get("VOLC_ACCESS_KEY_ID"),
        "VOLC_SECRET_ACCESS_KEY": os.environ.get("VOLC_SECRET_ACCESS_KEY"),
        "TOS_BUCKET": os.environ.get("TOS_BUCKET"),
    }
    has_tos = all(required.values())
    has_asr = bool(os.environ.get("DOUBAO_API_KEY")) or bool(
        os.environ.get("DOUBAO_APPID") and os.environ.get("DOUBAO_ACCESS_TOKEN")
    )
    region = os.environ.get("TOS_REGION") or "ap-southeast-1"
    endpoint = os.environ.get("TOS_S3_ENDPOINT") or f"tos-s3-{region}.volces.com"
    if endpoint and not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    endpoint_ok = bool(endpoint and urlparse(endpoint).hostname)
    checks = [
        DoctorCheck(
            "asr_dependency",
            dependency_ok,
            "Doubao TOS dependency is installed" if dependency_ok else "boto3 is missing",
            None if dependency_ok else "Run: pipx inject by2kb 'boto3>=1.34'",
        ),
        DoctorCheck(
            "asr_credentials",
            has_tos and has_asr,
            (
                "Doubao and TOS credentials are configured"
                if has_tos and has_asr
                else "Doubao or TOS credentials are incomplete"
            ),
            None
            if has_tos and has_asr
            else "Run by2kb init and configure TOS plus Doubao ASR credentials.",
        ),
        DoctorCheck(
            "asr_endpoint",
            endpoint_ok,
            "TOS endpoint is valid" if endpoint_ok else "TOS endpoint is missing or invalid",
            None if endpoint_ok else "Set TOS_S3_ENDPOINT to an http(s) endpoint.",
        ),
    ]
    access_ok = dependency_ok and has_tos and endpoint_ok and _tos_accessible()
    checks.append(
        DoctorCheck(
            "tos_access",
            access_ok,
            "Private TOS bucket is accessible" if access_ok else "Private TOS bucket access failed",
            None
            if access_ok
            else "Verify the bucket, endpoint, region, credentials, and HeadBucket permission.",
        )
    )
    return checks


def _tos_accessible() -> bool:
    try:
        import boto3
        from botocore.client import Config as BotoConfig

        region = os.environ.get("TOS_REGION") or "ap-southeast-1"
        endpoint = os.environ.get("TOS_S3_ENDPOINT") or f"tos-s3-{region}.volces.com"
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "https://" + endpoint
        client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get("VOLC_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("VOLC_SECRET_ACCESS_KEY"),
            config=BotoConfig(
                signature_version="s3v4",
                connect_timeout=3,
                read_timeout=5,
                retries={"max_attempts": 1},
                s3={"addressing_style": "virtual"},
            ),
        )
        client.head_bucket(Bucket=os.environ["TOS_BUCKET"])
    except Exception:
        return False
    return True


def _enrichment_checks(config: Config) -> list[DoctorCheck]:
    try:
        executor = config.resolved_enrichment_executor()
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if executor == "disabled":
        return [DoctorCheck("enrichment", True, "Raw-only mode is configured")]
    if executor == "api":
        base_url = urlparse(config.llm.base_url)
        ok = config.llm.usable and base_url.scheme in {"http", "https"} and bool(
            base_url.netloc
        )
        return [
            DoctorCheck(
                "enrichment",
                ok,
                "LLM API enrichment is configured" if ok else "LLM API configuration is incomplete",
                None
                if ok
                else "Set BY2KB_LLM_API_KEY, BY2KB_LLM_MODEL, and a valid base URL.",
            )
        ]

    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    plugin_root = hermes_home / "plugins" / "by2kb"
    plugin_files = (
        plugin_root / "plugin.yaml",
        plugin_root / "__init__.py",
        plugin_root / "skills" / "video-to-knowledge" / "SKILL.md",
    )
    plugin_ready = all(path.is_file() for path in plugin_files)
    by2kb_command = shutil.which("by2kb")
    return [
        DoctorCheck(
            "agent_command",
            by2kb_command is not None,
            (
                "by2kb command is callable by the Agent"
                if by2kb_command
                else "by2kb command is not on PATH"
            ),
            None if by2kb_command else "Install by2kb with pipx and restart the Agent.",
        ),
        DoctorCheck(
            "agent_integration",
            plugin_ready,
            (
                "Hermes by2kb plugin contract is installed"
                if plugin_ready
                else "Hermes by2kb plugin is incomplete or missing"
            ),
            None if plugin_ready else "Run: by2kb agent install hermes --force",
        ),
    ]
