"""Bounded output log for one command execution.

The file lives in a directory the command runtime creates and deletes. Callers
read by byte offset. The model never receives the path.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_BYTES = 1_048_576


@dataclass(frozen=True)
class OutputSlice:
    text: str
    offset: int
    next_offset: int | None
    stored_bytes: int
    storage_truncated: bool

    @property
    def truncated(self) -> bool:
        return self.next_offset is not None or self.storage_truncated


class CommandOutputLog:
    """Append-only log capped at ``max_bytes``. The retained prefix stays readable."""

    def __init__(self, directory: Path, command_id: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.path = directory / f"{command_id}.log"
        self.max_bytes = max_bytes
        self._size = 0
        self.storage_truncated = False
        self._lock = threading.Lock()
        descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self.storage_truncated or self._size >= self.max_bytes:
                self.storage_truncated = True
                return
            room = self.max_bytes - self._size
            chunk = data[:room]
            with self.path.open("ab") as handle:
                handle.write(chunk)
            self._size += len(chunk)
            if len(chunk) < len(data):
                self.storage_truncated = True

    def read(self, offset: int, limit: int) -> OutputSlice:
        if offset < 0 or limit < 0:
            raise ValueError("offset and limit must be >= 0")
        with self._lock:
            stored = self._size
            truncated = self.storage_truncated
            raw = self.path.read_bytes() if self.path.exists() else b""
        if offset > stored:
            offset = stored
        end = min(stored, offset + limit)
        end = _utf8_boundary(raw, offset, end)
        chunk = raw[offset:end]
        next_offset = None if end >= stored else end
        return OutputSlice(
            text=chunk.decode("utf-8", errors="replace"),
            offset=offset,
            next_offset=next_offset,
            stored_bytes=stored,
            storage_truncated=truncated,
        )

    def discard(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def _utf8_boundary(raw: bytes, start: int, end: int) -> int:
    """Keep ``end`` from splitting a code point when more bytes remain."""
    if end >= len(raw) or end <= start:
        return end
    while end > start and raw[end] & 0xC0 == 0x80:
        end -= 1
    return end


__all__ = ["DEFAULT_MAX_BYTES", "CommandOutputLog", "OutputSlice"]
