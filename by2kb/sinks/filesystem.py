from __future__ import annotations

import shutil
from pathlib import Path

from by2kb.sinks.base import SinkReceipt

_MARKDOWN_KIND = {"raw_md": ".raw.md", "updated_md": ".updated.md"}
_LEGACY_NAMES = {"raw_md": "raw.md", "updated_md": "updated.md"}


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
            if kind in _MARKDOWN_KIND:
                self._retire_previous(target_dir, kind)
        placed: dict[str, str] = {}
        for kind, source in artifact_paths.items():
            source_path = Path(str(source))
            destination = target_dir / source_path.name
            shutil.copyfile(source_path, destination)
            placed[kind] = str(destination)
        return SinkReceipt(sink=self.name, target=str(target_dir), artifacts=placed)

    def _retire_previous(self, target_dir: Path, kind: str) -> None:
        suffix = _MARKDOWN_KIND[kind]
        legacy = target_dir / _LEGACY_NAMES[kind]
        if legacy.is_file():
            legacy.unlink()
        for existing in target_dir.glob(f"*{suffix}"):
            existing.unlink()
