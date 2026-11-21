"""Atomic replacement for small configuration files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def replace_text(target: Path, text: str, *, mode: int) -> None:
    """Write ``text`` to a temporary file beside ``target`` and move it into place."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["replace_text"]
