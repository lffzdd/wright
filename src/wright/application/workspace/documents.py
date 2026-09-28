"""Store non-image uploads outside the edited workspace.

Text that decodes as UTF-8 is inlined into the user turn. Binary files are
named and hashed in the turn so the model is not told they were read.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...core.paths import session_dir

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_INLINE_CHARS = 100_000


class DocumentError(ValueError):
    pass


def store_document(session: Any, filename: str, data: bytes) -> dict[str, Any]:
    if not data:
        raise DocumentError("document is empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentError("document exceeds 5 MiB")
    name = Path(filename).name
    if not name or name in {".", ".."}:
        raise DocumentError("document filename is invalid")
    project = Path(getattr(session, "project_root", None) or session.workspace_dir)
    destination_dir = session_dir(project) / "documents" / str(session.session_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    document_id = f"doc_{uuid4().hex}"
    target = destination_dir / document_id
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    text = _inline_text(data)
    return {
        "id": document_id,
        "filename": name,
        "media_type": "text/plain" if text is not None else "application/octet-stream",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "storage_path": str(target.relative_to(session_dir(project))),
        "inlined": text is not None,
    }


def render_documents(session: Any, document_ids: list[str]) -> str:
    if not document_ids:
        return ""
    documents = getattr(session, "documents", {}) or {}
    project = Path(getattr(session, "project_root", None) or session.workspace_dir)
    root = session_dir(project)
    blocks: list[str] = []
    for document_id in document_ids:
        record = documents.get(document_id)
        if not isinstance(record, dict):
            raise DocumentError(f"unknown document: {document_id}")
        relative = str(record.get("storage_path") or "")
        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            raise DocumentError("document path escapes the session store")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record.get("sha256"):
            raise DocumentError("document bytes failed an integrity check")
        text = _inline_text(data)
        if text is None:
            blocks.append(
                f"[attached file {record.get('filename')} "
                f"sha256={record.get('sha256')} size={record.get('size')} binary]"
            )
        else:
            blocks.append(
                f"[attached file {record.get('filename')}]\n{text}"
            )
    return "\n\n".join(blocks)


def _inline_text(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return None
    if len(text) > MAX_INLINE_CHARS:
        return text[:MAX_INLINE_CHARS] + "\n…(truncated)"
    return text


__all__ = ["DocumentError", "render_documents", "store_document"]
