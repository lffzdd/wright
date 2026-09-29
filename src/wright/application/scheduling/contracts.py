"""Persistence ports used by job scheduling.

The shared SQLite store also keeps commands, interactions, and tool logs.
These ports are only the slices scheduling calls. One store object can
implement more than one of them. Transactions that span a definition, a run,
and the tool log stay inside the store method that already owns that write.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from ...domain.model.scheduling import JobDefinition, JobRun, TriggerSpec


class SchedulingError(ValueError):
    """A job definition or job run could not be read or changed."""


class JobNotFoundError(SchedulingError):
    """No job definition or job run matches the id in this session."""


@dataclass(frozen=True)
class JobChange:
    job: JobDefinition
    changed: bool


@dataclass(frozen=True)
class RunCancelResult:
    run: JobRun
    changed: bool
    cooperative: bool


@dataclass(frozen=True)
class RecordPage:
    records: tuple[Any, ...]
    next_cursor: str | None


class JobCatalog(Protocol):
    """Create and change job definitions. Pausing does not cancel live runs."""

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
    ) -> JobDefinition: ...

    def get_job(self, job_id: str) -> JobDefinition: ...

    def list_jobs(
        self, *, limit: int = 100, cursor: str | None = None, status: str | None = None
    ) -> RecordPage: ...

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
    ) -> JobChange: ...

    def pause_job(self, job_id: str) -> JobChange: ...

    def resume_job(self, job_id: str) -> JobChange: ...

    def cancel_job(self, job_id: str, reason: str) -> JobChange: ...

    def delete_job_if_unused(self, job_id: str) -> bool: ...


class JobRunLedger(Protocol):
    """Read job runs and cancel one run without touching future triggers."""

    def get_run(self, run_id: str) -> JobRun: ...

    def list_runs(
        self,
        job_id: str | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> RecordPage: ...

    def cancel_run(self, run_id: str, reason: str) -> RunCancelResult: ...


class JobDispatch(Protocol):
    """Materialize, claim, finish, and recover runs in one store transaction each."""

    session_id: str

    @property
    def closed(self) -> bool: ...

    def recover_interrupted(
        self, *, active_run_ids: Iterable[str] = (), now: float | None = None
    ) -> list[JobRun]: ...

    def emit_event(
        self, name: str, payload: dict[str, Any] | None = None, *, now: float | None = None
    ) -> int: ...

    def materialize_due(self, *, now: float | None = None) -> list[str]: ...

    def list_due_web_probes(self, *, now: float | None = None) -> list[JobDefinition]: ...

    def record_web_probe(
        self, job_id: str, snapshot: dict[str, Any], *, now: float | None = None
    ) -> str | None: ...

    def defer_web_probe(self, job_id: str, error: str, *, now: float | None = None) -> None: ...

    def count_active_runs(self) -> int: ...

    def claim_next_run(self, *, owner_id: str = "", now: float | None = None) -> JobRun | None: ...

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
        owner_id: str = "",
        now: float | None = None,
    ) -> JobRun: ...

    def get_run(self, run_id: str) -> JobRun: ...

    def has_active_job(self) -> bool: ...


class RunExecution(Protocol):
    """Start one claimed run and append its tool log. Not the job catalog."""

    def get_run(self, run_id: str) -> JobRun: ...

    def start_run(self, run_id: str, *, owner_id: str = "", now: float | None = None) -> JobRun: ...

    def set_run_root_turn(self, run_id: str, root_turn_id: str) -> JobRun: ...

    def is_cancel_requested(self, run_id: str) -> bool: ...

    def record_tool_intent(
        self,
        *,
        run_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        permission: dict[str, Any],
        environment: dict[str, Any],
        step_id: str = "",
        agent_task_id: str = "",
        session_run_id: str = "",
        parent_agent_task_id: str = "",
    ) -> str: ...

    def mark_tool_started(self, run_id: str, call_id: str) -> None: ...

    def record_tool_result(
        self, run_id: str, call_id: str, result: dict[str, Any], *, status: str
    ) -> None: ...

    def record_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        event_key: str = "",
    ) -> str: ...


__all__ = [
    "JobCatalog",
    "JobChange",
    "JobDispatch",
    "JobNotFoundError",
    "JobRunLedger",
    "RecordPage",
    "RunCancelResult",
    "RunExecution",
    "SchedulingError",
]
