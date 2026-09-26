"""File-backed core memory with serialized read-modify-write transactions."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ....domain.gateway.memory import ICoreMemoryStore
from ....domain.model.memory import CoreMemory
from .paths import memory_dir

CORE_MEMORY_FILE = "core_memory.json"
_locks_guard = threading.Lock()
_store_locks: dict[Path, threading.RLock] = {}


def _lock_for(directory: Path) -> threading.RLock:
    with _locks_guard:
        return _store_locks.setdefault(directory, threading.RLock())


class FileCoreMemoryStore(ICoreMemoryStore):
    """Stores core memory using a process lock and atomic file replacement."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or memory_dir()).expanduser().resolve()
        self.file_path = self.directory / CORE_MEMORY_FILE

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        # The lock file is stable: locking the JSON inode would stop protecting
        # the store after os.replace. Never unlink the lock file on release.
        with _lock_for(self.directory):
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.directory / ".core_memory.lock", os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "r+b", buffering=0) as lock_file:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(lock_file.fileno()).st_size == 0:
                        lock_file.write(b"\0")
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def load(self) -> CoreMemory:
        """Load CoreMemory from file, creating and persisting defaults if absent."""
        with self._exclusive_lock():
            missing = not self.file_path.is_file()
            memory = self._load_unlocked()
            if missing:
                self._save_unlocked(memory)
            return memory

    def save(self, core_memory: CoreMemory) -> None:
        """Replace the entire entity. Partial changes must use update()."""
        with self._exclusive_lock():
            self._save_unlocked(core_memory)

    def update(self, mutate: Callable[[CoreMemory], None]) -> CoreMemory:
        """Hold both thread and process locks until the mutation is persisted."""
        with self._exclusive_lock():
            memory = self._load_unlocked()
            mutate(memory)
            self._save_unlocked(memory)
            return memory

    def _load_unlocked(self) -> CoreMemory:
        if not self.file_path.is_file():
            return CoreMemory()
        data = json.loads(self.file_path.read_text(encoding="utf-8"))
        return CoreMemory(
            persona=data.get("persona") or CoreMemory().persona,
            human_profile=data.get("human_profile", ""),
            project_anchor=data.get("project_anchor", ""),
        )

    def _save_unlocked(self, core_memory: CoreMemory) -> None:
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
