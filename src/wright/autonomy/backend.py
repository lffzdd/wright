"""TaskService adapter for durable concrete-run records."""

from __future__ import annotations

import time
from typing import Callable

from ..tasks.service import TaskNotFoundError, TaskWaitCancelled
from ..tasks.types import RuntimeTask, TaskKind, TaskStatus
from .models import DurableRunRecord
from .store import AutonomyNotFoundError, AutonomyStore


class DurableTaskBackend:
    kind: TaskKind = "durable"

    def __init__(self, store: AutonomyStore) -> None:
        self.store = store

    def get(self, task_id: str) -> RuntimeTask | None:
        try:
            return self._project(self.store.get_run(task_id))
        except AutonomyNotFoundError:
            return None

    def list(self) -> list[RuntimeTask]:
        return [self._project(run) for run in self.store.list_runs()]

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
        try:
            return self._project(self.store.cancel_run(task_id, reason))
        except AutonomyNotFoundError as exc:
            raise TaskNotFoundError(str(exc)) from exc

    @staticmethod
    def _project(run: DurableRunRecord) -> RuntimeTask:
        status_map: dict[str, TaskStatus] = {
            "queued": "pending",
            "dispatched": "pending",
            "waiting_retry": "pending",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "unknown": "unknown",
        }
        return RuntimeTask(
            id=run.id,
            kind="durable",
            status=status_map[run.status],
            description=f"{run.automation_name}: {run.prompt}"[:8_000],
            root_turn_id=run.root_turn_id,
            created_at=run.created_at,
            started_at=run.started_at,
            ended_at=run.ended_at,
            result=run.result,
            error=run.error,
            cancel_requested=run.cancel_requested,
            cancel_reason=run.cancel_reason,
            details=run.to_dict(),
        )
