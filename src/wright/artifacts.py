"""Small managed artifact store for delivered files and MCP binary output."""

from __future__ import annotations

import base64
import os
import tempfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from .attachments import MAX_ATTACHMENT_BYTES, inspect_image
from .tools.base import ArtifactRef


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def register_file(self, source: Path, *, run_id: str, call_id: str, media_type: str) -> ArtifactRef:
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("artifact source must be a file")
        with source.open("rb") as handle:
            data = handle.read(MAX_ATTACHMENT_BYTES + 1)
        return self.register_bytes(
            data, name=source.name, media_type=media_type, run_id=run_id, call_id=call_id,
        )

    def register_bytes(
        self, data: bytes, *, name: str, run_id: str, call_id: str,
        media_type: str, max_bytes: int = MAX_ATTACHMENT_BYTES,
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
        self._validate_image(data, media_type)
        self.root.mkdir(parents=True, exist_ok=True)
        artifact_id = f"artifact_{uuid4().hex}"
        target = self.root / artifact_id
        fd, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return ArtifactRef(
            artifact_id, media_type, safe_name, len(data), run_id, call_id, target.name
        )

    def register_base64(self, data: str, **kwargs) -> ArtifactRef:
        max_bytes = kwargs.get("max_bytes", MAX_ATTACHMENT_BYTES)
        if len(data) > 4 * ((max_bytes + 2) // 3):
            raise ValueError("artifact exceeds size limit")
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception as exc:
            raise ValueError("invalid base64 artifact content") from exc
        return self.register_bytes(raw, **kwargs)

    def path_for(self, ref: ArtifactRef) -> Path:
        candidate = (self.root / ref.storage_path).resolve()
        if (ref.storage_path != ref.id or candidate.parent != self.root
                or not candidate.is_file()):
            raise FileNotFoundError(f"artifact is unavailable: {ref.id}")
        return candidate

    @staticmethod
    def _validate_image(data: bytes, media_type: str) -> None:
        if media_type.startswith("image/"):
            detected_type, _suffix, _width, _height = inspect_image(BytesIO(data))
            if detected_type != media_type:
                raise ValueError("artifact image content does not match its media type")

    def image_data_url(self, ref: ArtifactRef) -> str:
        """Read validated image bytes only at the transient provider boundary."""
        if not ref.media_type.startswith("image/"):
            raise ValueError("artifact is not an image")
        with self.path_for(ref).open("rb") as handle:
            data = handle.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES or len(data) != ref.size:
            raise ValueError("artifact bytes failed size check")
        self._validate_image(data, ref.media_type)
        return f"data:{ref.media_type};base64,{base64.b64encode(data).decode('ascii')}"
