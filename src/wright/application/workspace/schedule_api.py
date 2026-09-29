"""Schedule operations for one project database.

Live sessions use their own store and scheduler. A closed owner is opened
against the same database so a restart still shows the persisted status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.paths import task_db_path
from ...domain.model.agent.control import AgentControlPlane
from ...domain.model.scheduling import TriggerSpec
from ...infrastructure.persistence.autonomy_store import AutonomyStore
from ..execution.identity import bind_identity
from ..scheduling.contracts import SchedulingError
from ..scheduling.service import SchedulingService


class ScheduleApiError(ValueError):
    pass


def list_project_schedules(project_root: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    store = _reader(project_root)
    try:
        return [record.to_dict() for record in store.list_project_jobs(limit=limit)]
    finally:
        store.close()


def list_runs(project_root: Path, session_id: str, schedule_id: str) -> list[dict[str, Any]]:
    service, store = _service(project_root, session_id, scheduler=None)
    try:
        page = service.list_runs(schedule_id)
        return [record.to_dict() for record in page.records]
    except SchedulingError as exc:
        raise ScheduleApiError(str(exc)) from exc
    finally:
        store.close()


def create_schedule(runtime: Any, **fields: Any) -> dict[str, Any]:
    service = _from_runtime(runtime)
    trigger = TriggerSpec.from_dict(fields.pop("trigger"))
    record = service.create_job(trigger=trigger, **fields)
    return record.to_dict()


def update_schedule(runtime: Any, schedule_id: str, **fields: Any) -> dict[str, Any]:
    service = _from_runtime(runtime)
    if "trigger" in fields and fields["trigger"] is not None:
        fields["trigger"] = TriggerSpec.from_dict(fields["trigger"])
    try:
        result = service.update_job(schedule_id, **fields)
    except (SchedulingError, ValueError) as exc:
        raise ScheduleApiError(str(exc)) from exc
    return result.job.to_dict()


def pause_schedule(runtime: Any, schedule_id: str) -> dict[str, Any]:
    return _change(runtime, schedule_id, "pause")


def resume_schedule(runtime: Any, schedule_id: str) -> dict[str, Any]:
    return _change(runtime, schedule_id, "resume")


def project_action(
    project_root: Path,
    schedule_id: str,
    action: str,
    **fields: Any,
) -> dict[str, Any]:
    """Change a schedule from its persisted owner, without a selected workspace.

    The row lives in the project task database. A closed chat can still pause,
    resume, edit, or delete it. The next scheduler start reads that status.
    """

    owner = _owner_session(project_root, schedule_id)
    service, store = _service(project_root, owner, scheduler=None)
    try:
        if action == "pause":
            return service.pause_job(schedule_id).job.to_dict()
        if action == "resume":
            return service.resume_job(schedule_id).job.to_dict()
        if action == "delete":
            return service.delete_job(
                schedule_id,
                confirm=bool(fields.get("confirm")),
                reason="deleted from workspace",
            )
        if action == "update":
            trigger = fields.get("trigger")
            if trigger is not None:
                trigger = TriggerSpec.from_dict(trigger)
            return service.update_job(
                schedule_id,
                name=fields.get("name"),
                prompt=fields.get("prompt"),
                trigger=trigger,
                recovery_policy=fields.get("recovery_policy"),
                max_retries=fields.get("max_retries"),
                retry_delay_seconds=fields.get("retry_delay_seconds"),
            ).job.to_dict()
        raise ScheduleApiError("unsupported schedule action")
    except (SchedulingError, ValueError) as exc:
        raise ScheduleApiError(str(exc)) from exc
    finally:
        store.close()


def delete_schedule(runtime: Any, schedule_id: str, *, confirm: bool) -> dict[str, Any]:
    service = _from_runtime(runtime)
    try:
        return service.delete_job(
            schedule_id, confirm=confirm, reason="deleted from workspace",
        )
    except (SchedulingError, ValueError) as exc:
        raise ScheduleApiError(str(exc)) from exc


def _change(runtime: Any, schedule_id: str, action: str) -> dict[str, Any]:
    service = _from_runtime(runtime)
    try:
        result = (
            service.pause_job(schedule_id)
            if action == "pause"
            else service.resume_job(schedule_id)
        )
    except (SchedulingError, ValueError) as exc:
        raise ScheduleApiError(str(exc)) from exc
    return result.job.to_dict()


def _from_runtime(runtime: Any) -> SchedulingService:
    store = runtime.autonomy_store
    scheduler = getattr(getattr(runtime, "services", None), "job_scheduler", None)
    return SchedulingService(store, store, bind_identity(runtime.session_state, store), scheduler)


def _owner_session(project_root: Path, schedule_id: str) -> str:
    store = _reader(project_root)
    try:
        match = next(
            (record for record in store.list_project_jobs() if record.id == schedule_id),
            None,
        )
    finally:
        store.close()
    if match is None:
        raise ScheduleApiError("schedule was not found")
    return str(match.session_id)


def _reader(project_root: Path) -> AutonomyStore:
    return AutonomyStore(
        task_db_path(project_root),
        session_id="__project_list__",
        workspace_dir=project_root,
    )


def _service(project_root: Path, session_id: str, scheduler: Any) -> tuple[SchedulingService, AutonomyStore]:
    store = AutonomyStore(
        task_db_path(project_root),
        session_id=session_id,
        workspace_dir=project_root,
    )

    class _Closed:
        control_plane = AgentControlPlane()

        @staticmethod
        def get_command(_identifier: str):
            return None

    return SchedulingService(store, store, bind_identity(_Closed(), store), scheduler), store


__all__ = [
    "ScheduleApiError",
    "create_schedule",
    "delete_schedule",
    "list_project_schedules",
    "list_runs",
    "pause_schedule",
    "project_action",
    "resume_schedule",
    "update_schedule",
]
