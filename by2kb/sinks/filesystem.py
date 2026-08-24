from __future__ import annotations

import shutil
from pathlib import Path

from by2kb.sinks.base import SinkReceipt

_CURRENT_MARKDOWN_PATTERN = {
    "raw_md": "raw.*.md",
    "abstract_md": "short.*.md",
    "updated_md": "long.*.md",
}
_OLD_MARKDOWN_SUFFIX = {
    "raw_md": ".raw.md",
    "abstract_md": ".abstract.md",
    "updated_md": ".updated.md",
}
_CURRENT_PREFIXES = ("raw.", "short.", "long.")
_LEGACY_NAMES = {
    "raw_md": "raw.md",
    "abstract_md": "abstract.md",
    "updated_md": "updated.md",
}


class FilesystemSink:
    name = "filesystem"

    def __init__(self, library_root: Path):
        self._root = library_root

    async def publish(
        self, artifact_paths: dict[str, object], *, platform: str, video_id: str
    ) -> SinkReceipt:
        target_dir = self._root / platform / video_id
        target_dir.mkdir(parents=True, exist_ok=True)
        for kind in artifact_paths:
            if kind in _CURRENT_MARKDOWN_PATTERN:
                self._retire_previous(target_dir, kind)
        placed: dict[str, str] = {}
        for kind, source in artifact_paths.items():
            source_path = Path(str(source))
            destination = target_dir / source_path.name
            shutil.copyfile(source_path, destination)
            placed[kind] = str(destination)
        return SinkReceipt(sink=self.name, target=str(target_dir), artifacts=placed)

    def _retire_previous(self, target_dir: Path, kind: str) -> None:
        legacy = target_dir / _LEGACY_NAMES[kind]
        if legacy.is_file():
            legacy.unlink()
        for existing in target_dir.glob(_CURRENT_MARKDOWN_PATTERN[kind]):
            existing.unlink()
        legacy_pattern = f"*-{target_dir.name}{_OLD_MARKDOWN_SUFFIX[kind]}"
        for existing in target_dir.glob(legacy_pattern):
            # A current prefixed filename wins in the rare ambiguous case where
            # its title also ends with the complete old `-<video-id>.<kind>` form.
            if not existing.name.startswith(_CURRENT_PREFIXES):
                existing.unlink()
