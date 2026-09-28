"""Automation lifecycle operations for the autonomy store."""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ....domain.model.automation import AutomationRecord, TriggerSpec
from ._base import AutonomyNotFoundError, AutonomyStoreError, _StoreBase
from ._helpers import (
    StorePage,
    _bounded,
    _dump,
    _load_object,
    _safe_run_config,
    decode_page_cursor,
    encode_page_cursor,
    page_limit,
)


@dataclass(frozen=True)
class AutomationChange:
    automation: AutomationRecord
    changed: bool


class _AutomationsMixin(_StoreBase):
    def create_automation(
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
    ) -> AutomationRecord:
        name = _bounded(name, "name", 200)
        prompt = _bounded(prompt, "prompt", 8_000)
        if recovery_policy not in {"manual", "retry"}:
            raise AutonomyStoreError("recovery_policy must be manual or retry")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 20
        ):
            raise AutonomyStoreError("max_retries must be between 0 and 20")
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, (int, float))
            or retry_delay_seconds < 0
            or retry_delay_seconds > 86_400
        ):
            raise AutonomyStoreError(
                "retry_delay_seconds must be between 0 and 86400"
            )
        now = time.time() if now is None else float(now)
        config_json = _dump(_safe_run_config(run_config or {}))
        trigger = self._normalize_trigger(trigger)
        next_run_at = self._initial_next_run(trigger, now)
        trigger_state = (
            self._file_snapshot(trigger.path)
            if trigger.type == "file_change" else {}
        )
        automation_id = f"job_{secrets.token_hex(6)}"
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
                    automation_id, self.session_id, name, prompt, trigger.type,
                    _dump(trigger.to_dict()), _dump(trigger_state), recovery_policy,
                    int(max_retries), float(retry_delay_seconds), now, now,
                    next_run_at, config_json,
                ),
            )
        return self.get_automation(automation_id)

    def get_automation(self, automation_id: str) -> AutomationRecord:
        with self._read():
            row = self._conn.execute(
                "SELECT * FROM automations WHERE id = ? AND session_id = ?",
                (automation_id, self.session_id),
            ).fetchone()
        if row is None:
            raise AutonomyNotFoundError(f"Unknown schedule_id: {automation_id}")
        return self._automation_from_row(row)

    def list_automations(
        self, *, limit: int = 100, cursor: str | None = None, status: str | None = None
    ) -> StorePage:
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
        records = tuple(self._automation_from_row(row) for row in visible)
        next_cursor = None
        if extra and records:
            last = records[-1]
            next_cursor = encode_page_cursor(last.created_at, last.id)
        return StorePage(records, next_cursor)

    def has_active_automation(self) -> bool:
        with self._read():
            row = self._conn.execute(
                """SELECT 1 FROM automations
                   WHERE session_id = ? AND status = 'active' LIMIT 1""",
                (self.session_id,),
            ).fetchone()
        return row is not None

    def pause_automation(self, automation_id: str) -> AutomationChange:
        """Stop materializing new runs; already queued runs may still execute.

        The status check and update share one immediate transaction, so a
        concurrent cancel cannot be overwritten with paused.
        """
        now = time.time()
        with self._write():
            current = self._automation_for_update(automation_id)
            if current.status != "active":
                return AutomationChange(current, changed=False)
            cursor = self._conn.execute(
                """UPDATE automations SET status = 'paused', updated_at = ?
                   WHERE id = ? AND session_id = ? AND status = 'active'""",
                (now, automation_id, self.session_id),
            )
            updated = self._automation_for_update(automation_id)
            return AutomationChange(updated, changed=cursor.rowcount == 1)

    def resume_automation(
        self, automation_id: str, *, now: float | None = None
    ) -> AutomationChange:
        now = time.time() if now is None else float(now)
        with self._write():
            current = self._automation_for_update(automation_id)
            if current.status == "active":
                return AutomationChange(current, changed=False)
            if current.status != "paused":
                raise AutonomyStoreError(
                    f"schedule {automation_id} cannot resume from {current.status}"
                )
            next_run = self._resume_next_run(current.trigger, now)
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
                (now, next_run, _dump(state), automation_id, self.session_id),
            )
            if cursor.rowcount != 1:
                current = self._automation_for_update(automation_id)
                if current.status == "active":
                    return AutomationChange(current, changed=False)
                raise AutonomyStoreError(
                    f"schedule {automation_id} cannot resume from {current.status}"
                )
            return AutomationChange(self._automation_for_update(automation_id), changed=True)

    def cancel_automation(self, automation_id: str, reason: str) -> AutomationChange:
        now = time.time()
        reason = str(reason)[:1_000]
        with self._write():
            current = self._automation_for_update(automation_id)
            cursor = self._conn.execute(
                """UPDATE automations
                   SET status = 'cancelled', updated_at = ?, next_run_at = NULL
                   WHERE id = ? AND session_id = ? AND status != 'cancelled'""",
                (now, automation_id, self.session_id),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._conn.execute(
                    """UPDATE durable_runs
                       SET status = 'cancelled', ended_at = ?, cancel_requested = 1,
                           cancel_reason = ?
                       WHERE automation_id = ? AND session_id = ?
                         AND status IN ('queued', 'dispatched', 'waiting_retry')""",
                    (now, reason, automation_id, self.session_id),
                )
                self._conn.execute(
                    """UPDATE durable_runs
                       SET cancel_requested = 1, cancel_reason = ?
                       WHERE automation_id = ? AND session_id = ? AND status = 'running'""",
                    (reason, automation_id, self.session_id),
                )
            updated = self._automation_for_update(automation_id)
            if not changed and current.status == "cancelled":
                return AutomationChange(updated, changed=False)
            return AutomationChange(updated, changed=changed)

    def _automation_for_update(self, automation_id: str) -> AutomationRecord:
        row = self._conn.execute(
            "SELECT * FROM automations WHERE id = ? AND session_id = ?",
            (automation_id, self.session_id),
        ).fetchone()
        if row is None:
            raise AutonomyNotFoundError(f"Unknown schedule_id: {automation_id}")
        return self._automation_from_row(row)

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

    @staticmethod
    def _initial_next_run(trigger: TriggerSpec, now: float) -> float | None:
        # next_run_at is persisted as wall-clock epoch seconds. A backward
        # clock step therefore delays every interval until wall time catches
        # up; a monotonic clock cannot replace this without changing on-disk
        # semantics.
        if trigger.type == "once":
            return float(trigger.run_at or 0)
        if trigger.type == "interval":
            return (
                float(trigger.start_at)
                if trigger.start_at is not None
                else now + float(trigger.every_seconds or 0)
            )
        if trigger.type == "web_change":
            return now
        return None

    @staticmethod
    def _advance_interval(due: float, every_seconds: float, now: float) -> float:
        next_run = float(due)
        step = float(every_seconds)
        while next_run <= now:
            next_run += step
        return next_run

    @staticmethod
    def _resume_next_run(trigger: TriggerSpec, now: float) -> float | None:
        if trigger.type == "once":
            return max(now, float(trigger.run_at or now))
        if trigger.type == "interval":
            return now + float(trigger.every_seconds or 0)
        if trigger.type == "web_change":
            return now
        return None

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

    def _has_live_run_locked(self, automation_id: str) -> bool:
        row = self._conn.execute(
            """SELECT 1 FROM durable_runs WHERE automation_id = ?
               AND status IN ('queued', 'dispatched', 'running', 'waiting_retry') LIMIT 1""",
            (automation_id,),
        ).fetchone()
        return row is not None

    def _note_pending_event_locked(
        self,
        automation: AutomationRecord,
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
            automation = self._automation_from_row(row)
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
    def _automation_from_row(row: sqlite3.Row) -> AutomationRecord:
        return AutomationRecord(
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
