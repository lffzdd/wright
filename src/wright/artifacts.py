"""Small managed artifact store for delivered files and MCP binary output."""

from __future__ import annotations

import base64
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

    def register_bytes(
        self, data: bytes, *, name: str, run_id: str, call_id: str,
        media_type: str, max_bytes: int = 20 * 1024 * 1024,
    ) -> ArtifactRef:
        if not media_type or "/" not in media_type or media_type.startswith("/"):
            raise ValueError("invalid artifact media type")
        if not data:
            raise ValueError("artifact content is empty")
        if len(data) > max_bytes:
            raise ValueError("artifact exceeds 20 MiB limit")
        safe_name = Path(name).name or "artifact"
        if safe_name != name or safe_name in {".", ".."}:
            raise ValueError("unsafe artifact name")
        self.root.mkdir(parents=True, exist_ok=True)
        artifact_id = f"artifact_{uuid4().hex}"
        target = self.root / artifact_id
        target.write_bytes(data)
        return ArtifactRef(
            artifact_id, media_type, safe_name, len(data), run_id, call_id, target.name
        )

    def register_base64(self, data: str, **kwargs) -> ArtifactRef:
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception as exc:
            raise ValueError("invalid base64 artifact content") from exc
        return self.register_bytes(raw, **kwargs)

    def path_for(self, ref: ArtifactRef) -> Path:
        candidate = (self.root / ref.storage_path).resolve()
        if candidate.parent != self.root or not candidate.is_file():
            raise FileNotFoundError(f"artifact is unavailable: {ref.id}")
        return candidate
