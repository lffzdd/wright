"""Small managed artifact store for delivered files and MCP binary output."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from .tools.base import ArtifactRef


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def register_file(self, source: Path, *, run_id: str, call_id: str, media_type: str) -> ArtifactRef:
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("artifact source must be a file")
        self.root.mkdir(parents=True, exist_ok=True)
        artifact_id = f"artifact_{uuid4().hex}"
        target = self.root / artifact_id
        shutil.copyfile(source, target)
        return ArtifactRef(
            artifact_id, media_type, source.name, target.stat().st_size,
            run_id, call_id, target.name,
        )

    def path_for(self, ref: ArtifactRef) -> Path:
        candidate = (self.root / ref.storage_path).resolve()
        if candidate.parent != self.root or not candidate.is_file():
            raise FileNotFoundError(f"artifact is unavailable: {ref.id}")
        return candidate
