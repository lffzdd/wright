"""Gateway port for task execution backends."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..model.tasks import RuntimeTask, TaskKind


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
