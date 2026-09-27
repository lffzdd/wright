"""Scoped core-memory files with independent read-modify-write locks.

Global data stays in ``core_memory.json``. Each project anchor is
``core/projects/<project_id>.json``. A missing file is an empty section and
is not created by a read. A ``project_anchor`` key in the global file is ignored.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ....domain.gateway.memory import ICoreMemoryStore
from ....domain.model.memory.core import (
    DEFAULT_PERSONA,
    CoreMemoryStoreError,
    GlobalCoreRecord,
    ProjectCoreRecord,
)
from ....domain.model.memory.episode import PROJECT_ID_RE
from .paths import memory_dir

CORE_MEMORY_FILE = "core_memory.json"
PROJECT_CORE_DIRECTORY = "core/projects"
_GLOBAL_LOCK = ".core_memory.lock"
_locks_guard = threading.Lock()
_store_locks: dict[Path, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    resolved = path.expanduser().resolve()
    with _locks_guard:
        return _store_locks.setdefault(resolved, threading.RLock())


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def persona_from_payload(data: dict) -> str:
    raw = data.get("persona")
    if isinstance(raw, str) and raw.strip():
        return raw
    return DEFAULT_PERSONA


class FileCoreMemoryStore(ICoreMemoryStore):
    """File store. There is no process-wide current project on this object."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or memory_dir()).expanduser().resolve()
        self.file_path = self.directory / CORE_MEMORY_FILE
        self.projects_dir = self.directory / "core" / "projects"

    def global_path(self) -> Path:
        return self.file_path

    def project_path(self, project_id: str) -> Path:
        checked = _require_project_id(project_id)
        return self.projects_dir / f"{checked}.json"

    def project_lock_path(self, project_id: str) -> Path:
        checked = _require_project_id(project_id)
        return self.projects_dir / f".{checked}.lock"

    @contextmanager
    def _exclusive_lock(self, lock_path: Path) -> Iterator[None]:
        # The lock file is stable. Locking the JSON inode would stop protecting
        # the store after os.replace. Never unlink the lock file on release.
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if lock_path.parent == self.projects_dir:
            os.chmod(self.projects_dir, 0o700)
            os.chmod(self.projects_dir.parent, 0o700)
        with _lock_for(lock_path):
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
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

    def load_global(self) -> GlobalCoreRecord:
        if not self.file_path.is_file():
            return GlobalCoreRecord()
        with self._exclusive_lock(self.directory / _GLOBAL_LOCK):
            return self._load_global_unlocked()

    def update_global(self, mutate: Callable[[GlobalCoreRecord], None]) -> GlobalCoreRecord:
        with self._exclusive_lock(self.directory / _GLOBAL_LOCK):
            record = self._load_global_unlocked()
            persona = record.persona
            profile = record.human_profile
            mutate(record)
            record.persona = persona
            if record.human_profile != profile:
                self._save_global_unlocked(record)
            return record

    def load_project(self, project_id: str) -> ProjectCoreRecord:
        checked = _require_project_id(project_id)
        path = self.project_path(checked)
        if not path.is_file():
            return ProjectCoreRecord(project_id=checked)
        with self._exclusive_lock(self.project_lock_path(checked)):
            return self._load_project_unlocked(checked)

    def update_project(
        self, project_id: str, mutate: Callable[[ProjectCoreRecord], None],
    ) -> ProjectCoreRecord:
        checked = _require_project_id(project_id)
        with self._exclusive_lock(self.project_lock_path(checked)):
            record = self._load_project_unlocked(checked)
            before = record.project_anchor
            mutate(record)
            record.project_id = checked
            after = record.project_anchor
            if after != before:
                self._save_project_unlocked(record)
            return record

    def _load_global_unlocked(self) -> GlobalCoreRecord:
        if not self.file_path.is_file():
            return GlobalCoreRecord()
        data = json.loads(self.file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise CoreMemoryStoreError("core memory file is not an object")
        return GlobalCoreRecord(
            persona=persona_from_payload(data),
            human_profile=_text(data.get("human_profile")),
            updated_at=_text(data.get("updated_at")),
        )

    def _load_project_unlocked(self, project_id: str) -> ProjectCoreRecord:
        path = self.project_path(project_id)
        if not path.is_file():
            return ProjectCoreRecord(project_id=project_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise CoreMemoryStoreError("project core memory file is not an object")
        stored_id = data.get("project_id")
        if isinstance(stored_id, str) and stored_id and stored_id != project_id:
            raise CoreMemoryStoreError("project core memory file does not match project_id")
        return ProjectCoreRecord(
            project_id=project_id,
            project_anchor=_text(data.get("project_anchor")),
            updated_at=_text(data.get("updated_at")),
        )

    def _save_global_unlocked(self, record: GlobalCoreRecord) -> None:
        payload = {
            "persona": record.persona,
            "human_profile": record.human_profile,
            "updated_at": str(time.time()),
        }
        self._replace_json(self.directory, self.file_path, payload)

    def _save_project_unlocked(self, record: ProjectCoreRecord) -> None:
        payload = {
            "project_id": record.project_id,
            "project_anchor": record.project_anchor,
            "updated_at": str(time.time()),
        }
        self.projects_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.projects_dir, 0o700)
        self._replace_json(self.projects_dir, self.project_path(record.project_id), payload)

    def _replace_json(self, directory: Path, destination: Path, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        descriptor, tmp_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, destination)
            os.chmod(destination, 0o600)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


def _require_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise CoreMemoryStoreError("非法 project_id")
    return project_id


__all__ = [
    "CORE_MEMORY_FILE",
    "PROJECT_CORE_DIRECTORY",
    "CoreMemoryStoreError",
    "FileCoreMemoryStore",
    "persona_from_payload",
]
