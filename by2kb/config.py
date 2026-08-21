from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LLM_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ENV_PREFIX = "BY2KB_"


@dataclass
class LlmConfig:
    api_key: str | None = None
    base_url: str = DEFAULT_LLM_BASE_URL
    model: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.api_key and self.model)


@dataclass
class Config:
    home: Path
    library_root: Path
    db_path: Path
    skills: list[str] = field(default_factory=lambda: ["default-video-digest"])
    skills_dirs: list[Path] = field(default_factory=list)
    destination: str = "filesystem:library"
    preferred_languages: list[str] = field(default_factory=lambda: ["zh-CN", "zh", "en"])
    llm: LlmConfig = field(default_factory=LlmConfig)


def default_home() -> Path:
    return Path(os.environ.get(f"{ENV_PREFIX}HOME") or (Path.home() / ".by2kb"))


def load_config(home: Path | None = None) -> Config:
    base = home or default_home()
    data: dict = {}
    config_file = base / "config.toml"
    if config_file.is_file():
        data = tomllib.loads(config_file.read_text(encoding="utf-8"))

    def pick(key: str, default: object = None) -> object:
        return os.environ.get(f"{ENV_PREFIX}{key.upper()}") or data.get(key) or default

    library_root = Path(str(pick("library_root", str(base / "library")))).expanduser()
    skills_dirs = [Path(p).expanduser() for p in data.get("skills_dirs", [])]

    llm_section = data.get("llm", {}) if isinstance(data.get("llm"), dict) else {}
    llm = LlmConfig(
        api_key=os.environ.get(f"{ENV_PREFIX}LLM_API_KEY") or llm_section.get("api_key"),
        base_url=str(pick("llm_base_url", llm_section.get("base_url") or DEFAULT_LLM_BASE_URL)),
        model=os.environ.get(f"{ENV_PREFIX}LLM_MODEL") or llm_section.get("model"),
    )

    return Config(
        home=base,
        library_root=library_root,
        db_path=Path(str(pick("db_path", str(base / "by2kb.db")))).expanduser(),
        skills=list(data.get("skills", ["default-video-digest"])),
        skills_dirs=skills_dirs,
        destination=str(pick("destination", "filesystem:library")),
        preferred_languages=list(data.get("preferred_languages", ["zh-CN", "zh", "en"])),
        llm=llm,
    )
