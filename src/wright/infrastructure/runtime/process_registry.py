"""Live process handles. They are not checkpointed and they are not UI state."""

from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field

from .protocols import ProcessHandle


def terminate_process_tree(
    process: ProcessHandle,
    *,
    grace_seconds: float = 2.0,
) -> None:
    """Terminate a command and its descendants when it owns a process group."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except TimeoutError:
        process.terminate()
        try:
            process.wait(timeout=grace_seconds)
        except TimeoutError:
            return


@dataclass
class ProcessResources:
    """Non-serializable handles owned by this Python process only."""

    process: ProcessHandle
    output_lines: list[str]
    done: threading.Event
    output_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    reader_thread: threading.Thread | None = field(default=None, repr=False)


class ProcessRegistry:
    """Own live shell handles; Session persists records, never these objects."""

    def __init__(self) -> None:
        self._items: dict[str, ProcessResources] = {}
        self._lock = threading.RLock()

    def register(self, task_id: str, resources: ProcessResources) -> None:
        with self._lock:
            self._items[task_id] = resources

    def get(self, task_id: str) -> ProcessResources | None:
        with self._lock:
            return self._items.get(task_id)

    def discard(self, task_id: str) -> None:
        with self._lock:
            self._items.pop(task_id, None)

    def terminate_all(self) -> tuple[str, ...]:
        """Request shutdown for every process still owned by this runtime."""
        with self._lock:
            items = tuple(self._items.items())
        terminated: list[str] = []
        for task_id, resources in items:
            if not resources.done.is_set():
                terminate_process_tree(resources.process)
                terminated.append(task_id)
            if resources.reader_thread is not None:
                resources.reader_thread.join(timeout=2)
        return tuple(terminated)


@dataclass
class SessionProcesses:
    """Process-registry lookup for one session. UI snapshots are not stored here."""

    session_id: str
    process_registry: ProcessRegistry = field(default_factory=ProcessRegistry)

    def __post_init__(self) -> None:
        with _SESSIONS_LOCK:
            _SESSIONS[self.session_id] = self

    @classmethod
    def for_session(cls, session_id: str, *, create: bool = True) -> SessionProcesses | None:
        with _SESSIONS_LOCK:
            current = _SESSIONS.get(session_id)
            if current is not None or not create:
                return current
            return cls(session_id)


_SESSIONS: weakref.WeakValueDictionary[str, SessionProcesses] = weakref.WeakValueDictionary()
_SESSIONS_LOCK = threading.RLock()
