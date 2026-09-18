"""Small process-lifecycle helpers shared by shell execution and task control."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import weakref
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, ClassVar


def terminate_process_tree(
    process: subprocess.Popen,
    *,
    grace_seconds: float = 2.0,
) -> None:
    """Terminate a command and its descendants when it owns a process group."""
    if process.poll() is not None:
        return

    def send(sig: signal.Signals) -> None:
        try:
            process_group = os.getpgid(process.pid)
            if process_group == process.pid:
                os.killpg(process_group, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            # The caller can still observe a non-terminal task; never pretend
            # termination succeeded when the OS has not reaped the process.
            return


@dataclass
class ProcessResources:
    """Non-serializable handles owned by this Python process only."""

    process: subprocess.Popen
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
class RuntimeResources:
    """Live, process-local state addressed by a stable session id.

    It deliberately owns handles and streaming projections rather than putting
    them on :class:`Session`, whose data is checkpointed.  The weak registry is
    only an in-process lookup for TaskService; it never revives handles after a
    restart and does not retain a Session by itself.
    """

    session_id: str
    process_registry: ProcessRegistry = field(default_factory=ProcessRegistry)
    _response_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _response: dict[str, Any] | None = field(default=None, repr=False)

    _sessions: ClassVar[weakref.WeakValueDictionary[str, RuntimeResources]] = (
        weakref.WeakValueDictionary()
    )
    _sessions_lock: ClassVar[threading.RLock] = threading.RLock()

    def __post_init__(self) -> None:
        with self._sessions_lock:
            self._sessions[self.session_id] = self

    @classmethod
    def for_session(
        cls, session_id: str, *, create: bool = True
    ) -> RuntimeResources | None:
        with cls._sessions_lock:
            current = cls._sessions.get(session_id)
            if current is not None or not create:
                return current
            return cls(session_id)

    def begin_response(self, run_id: str) -> None:
        with self._response_lock:
            if self._response is None or self._response["run_id"] != run_id:
                self._response = {
                    "run_id": run_id,
                    "reasoning": "",
                    "content": "",
                    "tools": {},
                }

    def append_reasoning(self, piece: str) -> None:
        with self._response_lock:
            if self._response is not None:
                self._response["reasoning"] += piece

    def append_content(self, piece: str) -> None:
        with self._response_lock:
            if self._response is not None:
                self._response["content"] += piece

    def set_content(self, content: str) -> None:
        with self._response_lock:
            if self._response is not None:
                self._response["content"] = content

    def update_tool(self, call_id: str, values: dict[str, Any]) -> None:
        with self._response_lock:
            if self._response is None:
                return
            tool = self._response["tools"].setdefault(call_id, {})
            tool.update(deepcopy(values))

    def append_tool_output(self, call_id: str, output: str) -> None:
        with self._response_lock:
            if self._response is None:
                return
            tool = self._response["tools"].setdefault(call_id, {})
            tool["output"] = str(tool.get("output", "")) + output

    def response_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._response_lock:
            if self._response is None or self._response["run_id"] != run_id:
                return None
            snapshot = deepcopy(self._response)
        snapshot["tools"] = list(snapshot["tools"].values())
        return snapshot

    def finish_response(self, run_id: str) -> None:
        with self._response_lock:
            if self._response is not None and self._response["run_id"] == run_id:
                self._response = None

    def close(self) -> tuple[str, ...]:
        terminated = self.process_registry.terminate_all()
        with self._response_lock:
            self._response = None
        with self._sessions_lock:
            if self._sessions.get(self.session_id) is self:
                self._sessions.pop(self.session_id, None)
        return terminated
