"""Automation use cases.

The store performs the atomic transition. This service wakes the scheduler
after a change and returns that transition's snapshot. Tools do not read the
rule twice to guess whether it changed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ...infrastructure.persistence.autonomy_store import AutonomyStore
from ...infrastructure.persistence.autonomy_store._automations import AutomationChange
from ...infrastructure.persistence.autonomy_store._runs import RunCancelResult
from ..execution.identity import (
    ExecutionIdentity,
    ExecutionWaitCancelled,
)


class AutonomyService:
    def __init__(self, store: AutonomyStore, identity: ExecutionIdentity, scheduler=None) -> None:
        self._store = store
        self._identity = identity
        self._scheduler = scheduler

    def create_schedule(
        self,
        *,
        name: str,
        prompt: str,
        trigger,
        recovery_policy: str = "manual",
        max_retries: int = 0,
        retry_delay_seconds: float = 30,
        run_config: dict[str, Any] | None = None,
    ):
        record = self._store.create_automation(
            name=name,
            prompt=prompt,
            trigger=trigger,
            recovery_policy=recovery_policy,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
            run_config=run_config,
        )
        self._wake(True)
        return record

    def get_schedule(self, schedule_id: str):
        self._identity.require(schedule_id, "schedule")
        return self._store.get_automation(schedule_id)

    def list_schedules(
        self, *, status: str | None = None, limit: int = 100, cursor: str | None = None
    ):
        return self._store.list_automations(limit=limit, cursor=cursor, status=status)

    def pause_schedule(self, schedule_id: str) -> AutomationChange:
        self._identity.require(schedule_id, "schedule")
        result = self._store.pause_automation(schedule_id)
        self._wake(result.changed)
        return result

    def resume_schedule(self, schedule_id: str) -> AutomationChange:
        self._identity.require(schedule_id, "schedule")
        result = self._store.resume_automation(schedule_id)
        self._wake(result.changed)
        return result

    def cancel_schedule(self, schedule_id: str, reason: str) -> AutomationChange:
        self._identity.require(schedule_id, "schedule")
        result = self._store.cancel_automation(schedule_id, reason)
        self._wake(result.changed)
        return result

    def update_schedule(
        self,
        schedule_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        trigger=None,
        recovery_policy: str | None = None,
        max_retries: int | None = None,
        retry_delay_seconds: float | None = None,
    ) -> AutomationChange:
        self._identity.require(schedule_id, "schedule")
        result = self._store.update_automation(
            schedule_id,
            name=name,
            prompt=prompt,
            trigger=trigger,
            recovery_policy=recovery_policy,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
        )
        self._wake(result.changed)
        return result

    def delete_schedule(self, schedule_id: str, *, confirm: bool, reason: str) -> dict[str, Any]:
        """Cancel a schedule. A row with no runs is then removed.

        A schedule that already has runs stays as ``cancelled`` so its history
        remains addressable. This does not delete the workspace.
        """

        if not confirm:
            raise ValueError("confirmation is required")
        self._identity.require(schedule_id, "schedule")
        current = self._store.get_automation(schedule_id)
        cancelled = current
        changed = False
        if current.status != "cancelled":
            result = self._store.cancel_automation(schedule_id, reason)
            cancelled = result.automation
            changed = result.changed
            self._wake(changed)
        removed = self._store.delete_automation_if_unused(schedule_id)
        return {
            "id": schedule_id,
            "status": "deleted" if removed else cancelled.status,
            "deleted": removed,
            "cancelled": changed or cancelled.status == "cancelled",
            "retained_for_history": not removed,
        }

    def list_runs(
        self,
        schedule_id: str | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ):
        if schedule_id is not None:
            self._identity.require(schedule_id, "schedule")
        return self._store.list_runs(schedule_id, limit=limit, cursor=cursor)

    def get_run(self, run_id: str):
        self._identity.require(run_id, "run")
        return self._store.get_run(run_id)

    def wait_run(
        self,
        run_id: str,
        *,
        timeout: float,
        cancellation_check: Callable[[], bool] | None = None,
    ):
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        self._identity.require(run_id, "run")
        deadline = time.monotonic() + timeout
        while True:
            run = self._store.get_run(run_id)
            if run.terminal:
                return run
            if cancellation_check is not None and cancellation_check():
                raise ExecutionWaitCancelled(run_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return run
            time.sleep(min(0.05, remaining))

    def cancel_run(self, run_id: str, reason: str) -> tuple[RunCancelResult, str]:
        self._identity.require(run_id, "run")
        result = self._store.cancel_run(run_id, reason)
        self._wake(result.changed)
        schedule = self._store.get_automation(result.run.automation_id)
        return result, schedule.status

    def _wake(self, changed: bool) -> None:
        if changed and self._scheduler is not None:
            self._scheduler.notify_changed()


__all__ = ["AutonomyService"]
