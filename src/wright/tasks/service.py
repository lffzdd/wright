"""Task routing facade and adapters for the existing execution runtimes."""

from __future__ import annotations

import time
from typing import Callable, Protocol, Sequence, runtime_checkable

from ..coordination import AgentControlError, AgentTaskRecord
from ..processes import terminate_process_tree
from .types import RuntimeTask, TaskKind, TaskStatus


class TaskNotFoundError(ValueError):
    pass


class TaskWaitCancelled(RuntimeError):
    pass


@runtime_checkable
class TaskBackend(Protocol):
    """Adapter contract; a backend remains the owner of its task state."""

    kind: TaskKind

    def get(self, task_id: str) -> RuntimeTask | None: ...

    def list(self) -> list[RuntimeTask]: ...

    def wait(
        self,
        task_id: str,
        timeout: float | None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeTask: ...

    def cancel(self, task_id: str, reason: str) -> RuntimeTask: ...


class AgentTaskBackend:
    kind: TaskKind = "agent"

    def __init__(self, control_plane) -> None:
        self.control_plane = control_plane

    def get(self, task_id: str) -> RuntimeTask | None:
        try:
            return self._project(self.control_plane.get(task_id))
        except AgentControlError:
            return None

    def list(self) -> list[RuntimeTask]:
        rows = self.control_plane.snapshot().get("tasks", [])
        return [self._project(AgentTaskRecord.from_dict(row)) for row in rows]

    def wait(
        self,
        task_id: str,
        timeout: float | None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeTask:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            task = self.get(task_id)
            if task is None:
                raise TaskNotFoundError(f"Unknown task_id: {task_id}")
            if task.terminal:
                return task
            if cancellation_check is not None and cancellation_check():
                raise TaskWaitCancelled(f"wait_task cancelled: {task_id}")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return task
                time.sleep(min(0.05, remaining))
            else:
                time.sleep(0.05)

    def cancel(self, task_id: str, reason: str) -> RuntimeTask:
        task = self.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"Unknown task_id: {task_id}")
        if not task.terminal:
            self.control_plane.request_cancel(task_id, reason)
        updated = self.get(task_id)
        if updated is None:  # Defensive: control-plane records are not pruned here.
            raise TaskNotFoundError(f"Unknown task_id: {task_id}")
        return updated

    @staticmethod
    def _project(record: AgentTaskRecord) -> RuntimeTask:
        details = record.to_dict()
        return RuntimeTask(
            id=record.id,
            kind="agent",
            status=record.status,
            description=record.task,
            root_turn_id=record.root_turn_id,
            parent_id=record.parent_id,
            created_at=record.created_at,
            started_at=record.started_at,
            ended_at=record.ended_at,
            result=record.result,
            error=record.error,
            cancel_requested=record.cancel_requested,
            cancel_reason=record.cancel_reason,
            details=details,
        )


class ShellTaskBackend:
    kind: TaskKind = "shell"

    def __init__(self, session_state) -> None:
        self.session_state = session_state

    def get(self, task_id: str) -> RuntimeTask | None:
        task = self.session_state.get_background_task(task_id)
        return None if task is None else self._project(task)

    def list(self) -> list[RuntimeTask]:
        return [
            self._project(task)
            for task in self.session_state.list_background_tasks()
        ]

    def wait(
        self,
        task_id: str,
        timeout: float | None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeTask:
        raw = self.session_state.get_background_task(task_id)
        if raw is None:
            raise TaskNotFoundError(f"Unknown task_id: {task_id}")
        deadline = None if timeout is None else time.monotonic() + timeout
        while not raw.done.is_set():
            if cancellation_check is not None and cancellation_check():
                raise TaskWaitCancelled(f"wait_task cancelled: {task_id}")
            if deadline is None:
                raw.done.wait(timeout=0.05)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            raw.done.wait(timeout=min(0.05, remaining))
        return self._project(raw)

    def cancel(self, task_id: str, reason: str) -> RuntimeTask:
        task = self.session_state.get_background_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"Unknown task_id: {task_id}")
        if task.done.is_set():
            return self._project(task)

        task.cancel_requested = True
        task.cancel_reason = reason[:1_000]
        terminate_process_tree(task.process)
        task.done.wait(timeout=2)
        return self._project(task)

    @staticmethod
    def _project(task) -> RuntimeTask:
        done = task.done.is_set()
        returncode = task.process.returncode if done else None
        if not done:
            status: TaskStatus = "running"
        elif task.cancel_requested:
            status = "cancelled"
        elif returncode == 0:
            status = "completed"
        else:
            status = "failed"
        with task.output_lock:
            output = "".join(task.output_lines)[-8_000:]
        error = (
            f"命令以退出码 {returncode} 结束"
            if status == "failed" else ""
        )
        return RuntimeTask(
            id=task.task_id,
            kind="shell",
            status=status,
            description=task.command or "background shell command",
            root_turn_id=task.root_turn_id,
            created_at=task.created_at,
            started_at=task.started_at,
            ended_at=task.ended_at,
            output=output,
            error=error,
            returncode=returncode,
            cancel_requested=task.cancel_requested,
            cancel_reason=task.cancel_reason,
            details={
                "command": task.command,
                "done": done,
            },
        )


class TaskService:
    """Single control API that routes operations to runtime-specific owners."""

    def __init__(self, backends: Sequence[TaskBackend]) -> None:
        kinds = [backend.kind for backend in backends]
        if len(kinds) != len(set(kinds)):
            raise ValueError("TaskService backend kind must be unique")
        self.backends = tuple(backends)

    @classmethod
    def for_session(cls, session_state) -> "TaskService":
        backends: list[TaskBackend] = [
            AgentTaskBackend(session_state.control_plane),
            ShellTaskBackend(session_state),
        ]
        durable_store = getattr(session_state, "durable_task_store", None)
        if durable_store is not None:
            # Lazy import keeps the in-memory task facade usable without the
            # optional durable runtime being attached to a SessionState.
            from ..autonomy.backend import DurableTaskBackend

            backends.append(DurableTaskBackend(durable_store))
        return cls(backends)

    def get(self, task_id: str) -> RuntimeTask:
        _, task = self._resolve(task_id)
        return task

    def list(
        self,
        *,
        kind: TaskKind | None = None,
        status: TaskStatus | None = None,
        root_turn_id: str | None = None,
    ) -> list[RuntimeTask]:
        tasks = [
            task
            for backend in self.backends
            if kind is None or backend.kind == kind
            for task in backend.list()
            if status is None or task.status == status
            if root_turn_id is None or task.root_turn_id == root_turn_id
        ]
        return sorted(
            tasks,
            key=lambda task: (task.created_at or 0.0, task.id),
            reverse=True,
        )

    def wait(
        self,
        task_id: str,
        *,
        timeout: float | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeTask:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0")
        backend, _ = self._resolve(task_id)
        return backend.wait(task_id, timeout, cancellation_check)

    def cancel(self, task_id: str, *, reason: str) -> RuntimeTask:
        backend, _ = self._resolve(task_id)
        return backend.cancel(task_id, reason[:1_000])

    def _resolve(self, task_id: str) -> tuple[TaskBackend, RuntimeTask]:
        for backend in self.backends:
            task = backend.get(task_id)
            if task is not None:
                return backend, task
        raise TaskNotFoundError(f"Unknown task_id: {task_id}")
