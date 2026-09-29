"""Job definition lifecycle for the shared task store."""

from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from ....application.scheduling.contracts import JobChange, RecordPage
from ....domain.model.scheduling import (
    JobDefinition,
    TriggerSpec,
    checked_recovery,
    initial_next_run,
    resume_next_run,
)
from ._base import AutonomyNotFoundError, AutonomyStoreError, _StoreBase
from ._helpers import (
    _bounded,
    _dump,
    _load_object,
    _safe_run_config,
    decode_page_cursor,
    encode_page_cursor,
    page_limit,
)


def _recovery(policy: str, max_retries: int, retry_delay_seconds: float):
    try:
        return checked_recovery(policy, max_retries, retry_delay_seconds)
    except ValueError as exc:
        raise AutonomyStoreError(str(exc)) from exc


class _AutomationsMixin(_StoreBase):
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
        now: float | None = None,
    ) -> JobDefinition:
        name = _bounded(name, "name", 200)
        prompt = _bounded(prompt, "prompt", 8_000)
        recovery_policy, max_retries, retry_delay_seconds = _recovery(
            recovery_policy, max_retries, retry_delay_seconds
        )
        now = time.time() if now is None else float(now)
        config_json = _dump(_safe_run_config(run_config or {}))
        trigger = self._normalize_trigger(trigger)
        next_run_at = initial_next_run(trigger, now)
        trigger_state = (
            self._file_snapshot(trigger.path)
            if trigger.type == "file_change" else {}
        )
        job_id = f"job_{secrets.token_hex(6)}"
        with self._write():
            self._conn.execute(
                """
                INSERT INTO automations (
                    id, session_id, name, prompt, trigger_type, trigger_json,
                    trigger_state_json, status, recovery_policy, max_retries,
                    retry_delay_seconds, created_at, updated_at, next_run_at,
                    run_config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, self.session_id, name, prompt, trigger.type,
                    _dump(trigger.to_dict()), _dump(trigger_state), recovery_policy,
                    int(max_retries), float(retry_delay_seconds), now, now,
                    next_run_at, config_json,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobDefinition:
        with self._read():
            row = self._conn.execute(
                "SELECT * FROM automations WHERE id = ? AND session_id = ?",
                (job_id, self.session_id),
            ).fetchone()
        if row is None:
            raise AutonomyNotFoundError(f"Unknown schedule_id: {job_id}")
        return self._job_from_row(row)

    def list_jobs(
        self, *, limit: int = 100, cursor: str | None = None, status: str | None = None
    ) -> RecordPage:
        limit = page_limit(limit)
        predicate = "session_id = ?"
        parameters: list[Any] = [self.session_id]
        if status is not None:
            predicate += " AND status = ?"
            parameters.append(status)
        if cursor is not None:
            created_at, row_id = decode_page_cursor(cursor)
            predicate += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            parameters.extend((created_at, created_at, row_id))
        with self._read():
            rows = self._conn.execute(
                f"""SELECT * FROM automations WHERE {predicate}
                    ORDER BY created_at DESC, id DESC LIMIT ?""",
                (*parameters, limit + 1),
            ).fetchall()
        extra = len(rows) > limit
        visible = rows[:limit]
        records = tuple(self._job_from_row(row) for row in visible)
        next_cursor = None
        if extra and records:
            last = records[-1]
            next_cursor = encode_page_cursor(last.created_at, last.id)
        return RecordPage(records, next_cursor)

    def list_project_jobs(self, *, limit: int = 100) -> tuple:
        """Read every schedule in this project database, across session ids."""

        limit = page_limit(limit)
        with self._read():
            rows = self._conn.execute(
                "SELECT * FROM automations ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._job_from_row(row) for row in rows)

    def has_active_job(self) -> bool:
        with self._read():
            row = self._conn.execute(
                """SELECT 1 FROM automations
                   WHERE session_id = ? AND status = 'active' LIMIT 1""",
                (self.session_id,),
            ).fetchone()
        return row is not None

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
        now: float | None = None,
    ) -> JobChange:
        now = time.time() if now is None else float(now)
        with self._write():
            current = self._job_for_update(job_id)
            if current.status not in {"active", "paused"}:
                raise AutonomyStoreError(
                    f"schedule {job_id} cannot be edited from {current.status}"
                )
            next_name = current.name if name is None else _bounded(name, "name", 200)
            next_prompt = current.prompt if prompt is None else _bounded(prompt, "prompt", 8_000)
            next_trigger = current.trigger if trigger is None else self._normalize_trigger(trigger)
            next_policy, next_retries, next_delay = _recovery(
                current.recovery_policy if recovery_policy is None else recovery_policy,
                current.max_retries if max_retries is None else max_retries,
                current.retry_delay_seconds
                if retry_delay_seconds is None
                else retry_delay_seconds,
            )
            next_run = current.next_run_at
            if trigger is not None and current.status == "active":
                next_run = initial_next_run(next_trigger, now)
            cursor = self._conn.execute(
                """UPDATE automations
                   SET name = ?, prompt = ?, trigger_type = ?, trigger_json = ?,
                       recovery_policy = ?, max_retries = ?, retry_delay_seconds = ?,
                       next_run_at = ?, updated_at = ?
                   WHERE id = ? AND session_id = ? AND status IN ('active', 'paused')""",
                (
                    next_name, next_prompt, next_trigger.type, _dump(next_trigger.to_dict()),
                    next_policy, int(next_retries), float(next_delay), next_run, now,
                    job_id, self.session_id,
                ),
            )
            updated = self._job_for_update(job_id)
            return JobChange(updated, changed=cursor.rowcount == 1)

    def delete_job_if_unused(self, job_id: str) -> bool:
        """Remove a cancelled schedule that has no runs. History-bearing rows stay."""

        with self._write():
            current = self._job_for_update(job_id)
            if current.status != "cancelled":
                return False
            row = self._conn.execute(
                """SELECT 1 FROM durable_runs
                   WHERE automation_id = ? AND session_id = ? LIMIT 1""",
                (job_id, self.session_id),
            ).fetchone()
            if row is not None:
                return False
            cursor = self._conn.execute(
                """DELETE FROM automations
                   WHERE id = ? AND session_id = ? AND status = 'cancelled'""",
                (job_id, self.session_id),
            )
            return cursor.rowcount == 1

    def pause_job(self, job_id: str) -> JobChange:
        """Stop materializing new runs; already queued runs may still execute.

        The status check and update share one immediate transaction, so a
        concurrent cancel cannot be overwritten with paused.
        """
        now = time.time()
        with self._write():
            current = self._job_for_update(job_id)
            if current.status != "active":
                return JobChange(current, changed=False)
            cursor = self._conn.execute(
                """UPDATE automations SET status = 'paused', updated_at = ?
                   WHERE id = ? AND session_id = ? AND status = 'active'""",
                (now, job_id, self.session_id),
            )
            updated = self._job_for_update(job_id)
            return JobChange(updated, changed=cursor.rowcount == 1)

    def resume_job(
        self, job_id: str, *, now: float | None = None
    ) -> JobChange:
        now = time.time() if now is None else float(now)
        with self._write():
            current = self._job_for_update(job_id)
            if current.status == "active":
                return JobChange(current, changed=False)
            if current.status != "paused":
                raise AutonomyStoreError(
                    f"schedule {job_id} cannot resume from {current.status}"
                )
            next_run = resume_next_run(current.trigger, now)
            state = current.trigger_state
            if current.trigger.type == "file_change":
                state = self._file_snapshot(current.trigger.path)
            elif current.trigger.type == "web_change":
                state = {}
            cursor = self._conn.execute(
                """UPDATE automations
                   SET status = 'active', updated_at = ?, next_run_at = ?,
                       trigger_state_json = ?
                   WHERE id = ? AND session_id = ? AND status = 'paused'""",
                (now, next_run, _dump(state), job_id, self.session_id),
            )
            if cursor.rowcount != 1:
                current = self._job_for_update(job_id)
                if current.status == "active":
                    return JobChange(current, changed=False)
                raise AutonomyStoreError(
                    f"schedule {job_id} cannot resume from {current.status}"
                )
            return JobChange(self._job_for_update(job_id), changed=True)

    def cancel_job(self, job_id: str, reason: str) -> JobChange:
        now = time.time()
        reason = str(reason)[:1_000]
        with self._write():
            current = self._job_for_update(job_id)
            cursor = self._conn.execute(
                """UPDATE automations
                   SET status = 'cancelled', updated_at = ?, next_run_at = NULL
                   WHERE id = ? AND session_id = ? AND status != 'cancelled'""",
                (now, job_id, self.session_id),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._conn.execute(
                    """UPDATE durable_runs
                       SET status = 'cancelled', ended_at = ?, cancel_requested = 1,
                           cancel_reason = ?
                       WHERE automation_id = ? AND session_id = ?
                         AND status IN ('queued', 'dispatched', 'waiting_retry')""",
                    (now, reason, job_id, self.session_id),
                )
                self._conn.execute(
                    """UPDATE durable_runs
                       SET cancel_requested = 1, cancel_reason = ?
                       WHERE automation_id = ? AND session_id = ? AND status = 'running'""",
                    (reason, job_id, self.session_id),
                )
            updated = self._job_for_update(job_id)
            if not changed and current.status == "cancelled":
                return JobChange(updated, changed=False)
            return JobChange(updated, changed=changed)

    def _job_for_update(self, job_id: str) -> JobDefinition:
        row = self._conn.execute(
            "SELECT * FROM automations WHERE id = ? AND session_id = ?",
            (job_id, self.session_id),
        ).fetchone()
        if row is None:
            raise AutonomyNotFoundError(f"Unknown schedule_id: {job_id}")
        return self._job_from_row(row)

    def emit_event(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        now: float | None = None,
    ) -> int:
        name = _bounded(name, "event name", 200)
        payload_json = _dump(payload or {})
        if len(payload_json) > 20_000:
            raise AutonomyStoreError("event payload exceeds 20000 chars")
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                """INSERT INTO external_events
                   (session_id, name, payload_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (self.session_id, name, payload_json, now),
            )
            return int(cursor.lastrowid)

    def _normalize_trigger(self, trigger: TriggerSpec) -> TriggerSpec:
        if trigger.type != "file_change":
            return trigger
        candidate = Path(trigger.path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.workspace_dir / candidate).resolve()
        )
        if resolved != self.workspace_dir and self.workspace_dir not in resolved.parents:
            raise AutonomyStoreError("file trigger path must stay inside workspace")
        relative = str(resolved.relative_to(self.workspace_dir))
        return TriggerSpec(type="file_change", path=relative or ".")

    def _file_snapshot(self, relative_path: str) -> dict[str, Any]:
        path = (self.workspace_dir / relative_path).resolve()
        try:
            stat = path.stat()
            return {
                "exists": True,
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "is_dir": path.is_dir(),
            }
        except FileNotFoundError:
            return {"exists": False}

    def _has_live_run_locked(self, job_id: str) -> bool:
        row = self._conn.execute(
            """SELECT 1 FROM durable_runs WHERE automation_id = ?
               AND status IN ('queued', 'dispatched', 'running', 'waiting_retry') LIMIT 1""",
            (job_id,),
        ).fetchone()
        return row is not None

    def _note_pending_event_locked(
        self,
        automation: JobDefinition,
        payload: dict[str, Any],
        now: float,
    ) -> None:
        state = dict(automation.trigger_state)
        pending = dict(state.get("pending_event") or {})
        count = int(pending.get("count") or 0) + 1
        state["pending_event"] = {**payload, "count": count}
        self._conn.execute(
            """UPDATE automations SET trigger_state_json = ?, updated_at = ?
               WHERE id = ?""",
            (_dump(state), now, automation.id),
        )

    def _flush_pending_events_locked(
        self, created: list[str], now: float
    ) -> None:
        rows = self._conn.execute(
            """SELECT * FROM automations
               WHERE session_id = ? AND status = 'active'
                 AND trigger_type = 'event'""",
            (self.session_id,),
        ).fetchall()
        for row in rows:
            automation = self._job_from_row(row)
            pending = automation.trigger_state.get("pending_event")
            if not isinstance(pending, dict) or not pending:
                continue
            if self._has_live_run_locked(automation.id):
                continue
            created.append(self._insert_run_locked(
                automation,
                scheduled_for=now,
                trigger_payload={
                    "event_id": pending.get("event_id"),
                    "event_name": pending.get("event_name"),
                    "payload": pending.get("payload") or {},
                    "coalesced_count": int(pending.get("count") or 1),
                },
                occurrence_key=f"event:{pending.get('event_id')}",
                now=now,
            ))
            state = dict(automation.trigger_state)
            state.pop("pending_event", None)
            self._conn.execute(
                """UPDATE automations SET trigger_state_json = ?,
                   last_run_at = ?, updated_at = ? WHERE id = ?""",
                (_dump(state), now, now, automation.id),
            )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobDefinition:
        return JobDefinition(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            name=str(row["name"]),
            prompt=str(row["prompt"]),
            trigger=TriggerSpec.from_dict(_load_object(row["trigger_json"])),
            status=row["status"],
            recovery_policy=row["recovery_policy"],
            max_retries=int(row["max_retries"]),
            retry_delay_seconds=float(row["retry_delay_seconds"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            next_run_at=(
                None if row["next_run_at"] is None else float(row["next_run_at"])
            ),
            last_run_at=(
                None if row["last_run_at"] is None else float(row["last_run_at"])
            ),
            trigger_state=_load_object(row["trigger_state_json"]),
            run_config=_load_object(row["run_config_json"]),
        )
