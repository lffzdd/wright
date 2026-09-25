"""File-backed persistence adapter for CoreMemory (.wright/core_memory.json)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ...domain.gateway.core_memory_gateway import ICoreMemoryStore
from ...domain.model.core_memory import CoreMemory
from .memory_paths import memory_dir

CORE_MEMORY_FILE = "core_memory.json"
_locks_guard = threading.Lock()
_store_locks: dict[Path, threading.RLock] = {}


def _lock_for(directory: Path) -> threading.RLock:
    with _locks_guard:
        return _store_locks.setdefault(directory, threading.RLock())


class FileCoreMemoryStore(ICoreMemoryStore):
    """Stores CoreMemory as an atomic JSON file."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or memory_dir()).expanduser().resolve()
        self.file_path = self.directory / CORE_MEMORY_FILE

    def load(self) -> CoreMemory:
        """Load CoreMemory from file, creating and persisting defaults if absent."""
        with _lock_for(self.directory):
            if not self.file_path.is_file():
                default_mem = CoreMemory()
                self.save(default_mem)
                return default_mem
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                return CoreMemory(
                    persona=data.get("persona") or CoreMemory().persona,
                    human_profile=data.get("human_profile", ""),
                    project_anchor=data.get("project_anchor", ""),
                )
            except Exception:
                default_mem = CoreMemory()
                return default_mem

    def save(self, core_memory: CoreMemory) -> None:
        """Atomically persist CoreMemory to JSON file."""
        with _lock_for(self.directory):
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = core_memory.to_dict()
            payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            descriptor, tmp_path = tempfile.mkstemp(
                prefix=".core_memory.",
                suffix=".tmp",
                dir=self.directory,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self.file_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise


__all__ = ["CORE_MEMORY_FILE", "FileCoreMemoryStore"]
