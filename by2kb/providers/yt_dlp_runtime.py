"""Shared offline JS runtime selection for ingestion, search and diagnostics."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib import metadata

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

from by2kb.errors import ConfigError

REPAIR_DEPENDENCIES = (
    'Repair using the original package manager: pipx inject by2kb "yt-dlp[default]>=2026.8.19" '
    "--upgrade, or uv tool upgrade by2kb. EJS must match yt-dlp; do not install an arbitrary EJS version."
)
RUNTIME_HELP = (
    "Install Deno 2.3+ or Node.js 22+ with a trusted package manager and expose it to the gateway PATH. "
    "Alternatively set [sources.yt_dlp] js_runtime and js_runtime_path. No browser or login is required "
    "to install JS support; platform login restrictions may still apply."
)


@dataclass(frozen=True)
class JsSettings:
    runtime: str = "auto"
    path: str = ""

    @classmethod
    def from_mapping(cls, values: dict | None = None) -> "JsSettings":
        values = values or {}
        runtime = str(os.environ.get("BY2KB_YTDLP_JS_RUNTIME") or values.get("js_runtime") or "auto").strip().lower()
        path = str(os.environ.get("BY2KB_YTDLP_JS_RUNTIME_PATH") or values.get("js_runtime_path") or "").strip()
        if runtime not in {"auto", "deno", "node", "none"}:
            raise ConfigError("yt-dlp js_runtime must be auto, deno, node, or none")
        if path and runtime in {"auto", "none"}:
            raise ConfigError("yt-dlp js_runtime_path requires an explicit deno or node runtime")
        return cls(runtime, path)

    def to_mapping(self) -> dict[str, str]:
        return {"js_runtime": self.runtime, "js_runtime_path": self.path}


@dataclass(frozen=True)
class RuntimeStatus:
    name: str | None = None
    path: str | None = None
    version: str | None = None
    message: str = "No supported JavaScript runtime found"
    disabled: bool = False

    def options(self) -> dict:
        # Explicitly enable Node too: upstream only enables Deno automatically.
        return {"js_runtimes": {self.name: {"path": self.path}} if self.name else {}}


def runtime_status(settings: JsSettings) -> RuntimeStatus:
    if settings.runtime == "none":
        return RuntimeStatus(message="YouTube JS support is explicitly disabled; extraction may be limited", disabled=True)
    names = ("deno", "node") if settings.runtime == "auto" else (settings.runtime,)
    for name in names:
        executable = shutil.which(os.path.expanduser(settings.path) if settings.path else name)
        if not executable:
            continue
        try:
            result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                    timeout=3, check=False)
            match = re.search(r"(?:^|\s)v?(\d+\.\d+\.\d+)\b", result.stdout)
            if result.returncode or not match:
                continue
            version = Version(match.group(1))
            if version < Version("2.3.0" if name == "deno" else "22.0.0"):
                continue
            return RuntimeStatus(name, executable, str(version), f"YouTube JS runtime: {name} {version}")
        except (OSError, subprocess.SubprocessError, InvalidVersion):
            continue
    return RuntimeStatus()


def js_options(settings: JsSettings) -> dict:
    status = runtime_status(settings)
    if settings.runtime not in {"auto", "none"} and not status.name:
        raise ConfigError("Configured YouTube JS runtime is unavailable or too old. " + RUNTIME_HELP)
    return status.options()


def ejs_status() -> tuple[bool, str]:
    """Check the installed EJS version against this yt-dlp distribution's requirement."""
    try:
        installed = Version(metadata.version("yt-dlp-ejs"))
        dependencies = metadata.requires("yt-dlp") or []
        requirements = [Requirement(value) for value in dependencies]
        requirements = [r for r in requirements if r.name.replace("_", "-").lower() == "yt-dlp-ejs"
                        and (r.marker is None or r.marker.evaluate({"extra": "default"}))]
        if not requirements:
            return False, "Installed yt-dlp does not declare compatible EJS support; upgrade yt-dlp[default]"
        if not all(installed in r.specifier for r in requirements):
            return False, "yt-dlp-ejs is incompatible with the installed yt-dlp; upgrade them together"
        return True, f"Compatible YouTube EJS scripts are installed ({installed})"
    except (metadata.PackageNotFoundError, InvalidVersion, ValueError):
        return False, "Compatible YouTube EJS scripts are missing or cannot be verified"
