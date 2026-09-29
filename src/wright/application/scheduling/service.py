"""Job definition and job run use cases.

The store performs the atomic transition. This service wakes the scheduler
after a change and returns that transition's snapshot.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ...domain.model.scheduling import TriggerSpec
from ..execution.identity import (
    ExecutionIdentity,
    ExecutionWaitCancelled,
)
from .contracts import (
    JobCatalog,
    JobChange,
    JobRun,
    JobRunLedger,
    RecordPage,
    RunCancelResult,
)
from .scheduler import JobScheduler


class SchedulingService:
    def __init__(
        self,
        catalog: JobCatalog,
        runs: JobRunLedger,
        identity: ExecutionIdentity,
        scheduler: JobScheduler | None = None,
    ) -> None:
        self._catalog = catalog
        self._runs = runs
        self._identity = identity
        self._scheduler = scheduler

    def create_job(
        self,
        *,
        name: str,
        prompt: str,
        trigger: TriggerSpec,
        recovery_policy: str = "manual",
        max_retries: int = 0,
        retry_delay_seconds: float = 30,
        run_config: dict[str, Any] | None = None,
    ):
        record = self._catalog.create_job(
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

    def get_job(self, job_id: str):
        self._identity.require(job_id, "schedule")
        return self._catalog.get_job(job_id)

    def list_jobs(
        self, *, status: str | None = None, limit: int = 100, cursor: str | None = None
    ) -> RecordPage:
        return self._catalog.list_jobs(limit=limit, cursor=cursor, status=status)

    def pause_job(self, job_id: str) -> JobChange:
        self._identity.require(job_id, "schedule")
        result = self._catalog.pause_job(job_id)
        self._wake(result.changed)
        return result

    def resume_job(self, job_id: str) -> JobChange:
        self._identity.require(job_id, "schedule")
        result = self._catalog.resume_job(job_id)
        self._wake(result.changed)
        return result

    def cancel_job(self, job_id: str, reason: str) -> JobChange:
        self._identity.require(job_id, "schedule")
        result = self._catalog.cancel_job(job_id, reason)
        self._wake(result.changed)
        return result

    def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        trigger: TriggerSpec | None = None,
        recovery_policy: str | None = None,
        max_retries: int | None = None,
        retry_delay_seconds: float | None = None,
    ) -> JobChange:
        self._identity.require(job_id, "schedule")
        result = self._catalog.update_job(
            job_id,
            name=name,
            prompt=prompt,
            trigger=trigger,
            recovery_policy=recovery_policy,
            max_retries=max_retries,
            retry_delay_seconds=retry_delay_seconds,
        )
        self._wake(result.changed)
        return result

    def delete_job(self, job_id: str, *, confirm: bool, reason: str) -> dict[str, Any]:
        """Cancel a job definition. A row with no runs is then removed.

        A definition that already has runs stays ``cancelled`` so its history
        remains addressable. This does not delete the workspace.
        """

        if not confirm:
            raise ValueError("confirmation is required")
        self._identity.require(job_id, "schedule")
        current = self._catalog.get_job(job_id)
        cancelled = current
        changed = False
        if current.status != "cancelled":
            result = self._catalog.cancel_job(job_id, reason)
            cancelled = result.job
            changed = result.changed
            self._wake(changed)
        removed = self._catalog.delete_job_if_unused(job_id)
        return {
            "id": job_id,
            "status": "deleted" if removed else cancelled.status,
            "deleted": removed,
            "cancelled": changed or cancelled.status == "cancelled",
            "retained_for_history": not removed,
        }

    def list_runs(
        self,
        job_id: str | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> RecordPage:
        if job_id is not None:
            self._identity.require(job_id, "schedule")
        return self._runs.list_runs(job_id, limit=limit, cursor=cursor)

    def get_run(self, run_id: str) -> JobRun:
        self._identity.require(run_id, "run")
        return self._runs.get_run(run_id)

    def wait_run(
        self,
        run_id: str,
        *,
        timeout: float,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> JobRun:
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        self._identity.require(run_id, "run")
        deadline = time.monotonic() + timeout
        while True:
            run = self._runs.get_run(run_id)
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
        result = self._runs.cancel_run(run_id, reason)
        self._wake(result.changed)
        job = self._catalog.get_job(result.run.job_id)
        return result, job.status

    def _wake(self, changed: bool) -> None:
        if changed and self._scheduler is not None:
            self._scheduler.notify_changed()


__all__ = ["SchedulingService"]
