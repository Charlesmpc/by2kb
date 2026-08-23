from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LLM_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ENV_PREFIX = "BY2KB_"
ENRICHMENT_EXECUTORS = frozenset({"auto", "api", "external_agent", "disabled"})


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
    abstract_skill: str = "short-video-abstract"
    study_skill: str | None = None
    skills_dirs: list[Path] = field(default_factory=list)
    destination: str = "filesystem:library"
    preferred_languages: list[str] = field(default_factory=lambda: ["zh-CN", "zh", "en"])
    asr_provider: str = "doubao_auc"
    enrichment_executor: str = "auto"
    llm: LlmConfig = field(default_factory=LlmConfig)

    def resolved_enrichment_executor(self, override: str | None = None) -> str:
        executor = override or self.enrichment_executor
        if executor == "auto":
            return "api" if self.llm.usable else "disabled"
        if executor not in ENRICHMENT_EXECUTORS:
            raise ValueError(f"unknown enrichment executor: {executor}")
        return executor


def default_home() -> Path:
    return Path(os.environ.get(f"{ENV_PREFIX}HOME") or (Path.home() / ".by2kb"))


def load_env_file(path: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    env_file_var = os.environ.get(f"{ENV_PREFIX}ENV_FILE")
    if env_file_var:
        candidates.append(Path(env_file_var))
    candidates.append(default_home() / ".env")
    candidates.append(Path.cwd() / ".env")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and value and key not in os.environ:
                os.environ[key] = value
        return candidate
    return None


def load_config(home: Path | None = None) -> Config:
    load_env_file()
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
    asr_section = data.get("asr", {}) if isinstance(data.get("asr"), dict) else {}
    enrichment_section = (
        data.get("enrichment", {}) if isinstance(data.get("enrichment"), dict) else {}
    )
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
        abstract_skill=str(
            pick(
                "abstract_skill",
                enrichment_section.get("abstract_profile") or "short-video-abstract",
            )
        ),
        study_skill=str(
            pick(
                "study_skill",
                enrichment_section.get("study_profile")
                or (data.get("skills") or ["default-video-digest"])[0],
            )
        ),
        skills_dirs=skills_dirs,
        destination=str(pick("destination", "filesystem:library")),
        preferred_languages=list(data.get("preferred_languages", ["zh-CN", "zh", "en"])),
        asr_provider=str(pick("asr_provider", asr_section.get("provider") or "doubao_auc")),
        enrichment_executor=str(
            pick(
                "enrichment_executor",
                enrichment_section.get("executor") or "auto",
            )
        ),
        llm=llm,
    )
