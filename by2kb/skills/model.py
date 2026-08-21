from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class Skill(BaseModel):
    name: str
    description: str = ""
    version: str = "0.0.0"
    body: str = ""
    path: Path | None = None


def parse_skill_file(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    frontmatter: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    frontmatter[key.strip()] = value.strip()
            body = parts[2].lstrip("\n")
    return Skill(
        name=frontmatter.get("name") or path.parent.name,
        description=frontmatter.get("description", ""),
        version=frontmatter.get("version", "0.0.0"),
        body=body,
        path=path,
    )


def find_skill(name: str, dirs: list[Path]) -> Skill | None:
    for directory in dirs:
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*/SKILL.md")):
            skill = parse_skill_file(candidate)
            if skill.name == name:
                return skill
    return None
