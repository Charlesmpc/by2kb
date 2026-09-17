from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib.resources import as_file, files
from pathlib import Path

from by2kb.errors import ConfigError


def install_hermes_plugin(
    *,
    hermes_home: Path | None = None,
    force: bool = False,
    enable: bool = True,
) -> Path:
    base = (hermes_home or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))).expanduser().resolve()
    target = base / "plugins" / "by2kb"
    # Never replace a git/catalog pin owned by Hermes, even with --force.
    _check_install_ownership(base, target)
    if target.exists():
        if not force:
            raise ConfigError(
                f"Hermes plugin already exists: {target}; use --force to replace it"
            )
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    resource = files("by2kb.integrations.hermes")
    with as_file(resource) as source:
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    if enable:
        hermes = shutil.which("hermes")
        if not hermes:
            raise ConfigError(
                f"plugin copied to {target}, but the hermes command is not on PATH; "
                "run `hermes plugins enable by2kb` manually"
            )
        completed = subprocess.run(
            [hermes, "plugins", "enable", "by2kb"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env={**os.environ, "HERMES_HOME": str(base)},
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ConfigError(f"Hermes could not enable by2kb: {detail}")
    return target


def _check_install_ownership(base: Path, target: Path) -> None:
    if target.is_symlink() or target.resolve() != base / "plugins" / "by2kb":
        raise ConfigError("Refusing to replace a linked Hermes plugin directory")
    metadata_path = base / "plugins" / ".install-metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("Cannot verify Hermes plugin ownership; inspect .install-metadata.json before installing") from exc
        if not isinstance(metadata, dict):
            raise ConfigError("Cannot verify Hermes plugin ownership: invalid install metadata")
        managed = "by2kb" in metadata
    else:
        managed = False
    if managed or (target / ".hermes-catalog.json").exists() or (target / ".git").exists():
        raise ConfigError(
            "by2kb is managed by Hermes; refusing to overwrite its reviewed/pinned code. "
            "For catalog installs use `hermes plugins update by2kb`; for a pinned Git "
            "install use Hermes install with an explicit --ref. Do not use by2kb --force."
        )
