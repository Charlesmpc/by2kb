from __future__ import annotations

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
    base = hermes_home or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    target = base / "plugins" / "by2kb"
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
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ConfigError(f"Hermes could not enable by2kb: {detail}")
    return target
