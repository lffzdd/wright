"""Durable run scheduling, ownership, completion, and recovery."""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from ..models import AutomationRecord, DurableRunRecord
from ._base import AutonomyNotFoundError, AutonomyStoreError, _StoreBase
from ._helpers import _dump, _load_object


class _RunsMixin(_StoreBase):
    def get_run(self, run_id: str) -> DurableRunRecord:
        with self._read():
            row = self._run_query("r.id = ?", (run_id,)).fetchone()
        if row is None:
            raise AutonomyNotFoundError(f"Unknown task_id: {run_id}")
        return self._run_from_row(row)

    def list_runs(self, automation_id: str | None = None) -> list[DurableRunRecord]:
        if automation_id is not None:
            self.get_automation(automation_id)
        with self._read():
            if automation_id is None:
                rows = self._run_query("1 = 1", ()).fetchall()
            else:
                rows = self._run_query(
                    "r.automation_id = ?", (automation_id,)
                ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def claim_next_run(
        self, *, owner_id: str = "", now: float | None = None
    ) -> DurableRunRecord | None:
        now = time.time() if now is None else float(now)
        with self._write():
            while True:
                row = self._conn.execute(
                    """SELECT id FROM durable_runs
                       WHERE session_id = ?
                         AND status IN ('queued', 'waiting_retry')
                         AND scheduled_for <= ?
                       ORDER BY scheduled_for, created_at LIMIT 1""",
                    (self.session_id, now),
                ).fetchone()
                if row is None:
                    return None
                run_id = str(row["id"])
                effects = self._conn.execute(
                    "SELECT status FROM durable_tool_executions WHERE run_id = ? AND status != 'intended'",
                    (run_id,),
                ).fetchall()
                if not effects:
                    break
                # Old versions could persist waiting_retry after an effect.
                # Refuse these records at the claim boundary as well as finish.
                unresolved = any(effect["status"] in {"started", "unknown"} for effect in effects)
                reason = "previous tool execution prevents automatic whole-run replay"
                self._conn.execute(
                    """UPDATE durable_tool_executions
                       SET status = 'unknown', ended_at = ?, result_json = ?
                       WHERE run_id = ? AND status = 'started'""",
                    (now, _dump({"error": reason}), run_id),
                )
                self._conn.execute(
                    """UPDATE durable_runs SET status = ?, ended_at = ?, result = '', error = ?
                       WHERE id = ? AND status IN ('queued', 'waiting_retry')""",
                    ("unknown" if unresolved else "failed", now, reason, run_id),
                )
            cursor = self._conn.execute(
                """UPDATE durable_runs
                   SET status = 'dispatched', ended_at = NULL, owner_id = ?
                   WHERE id = ? AND status IN ('queued', 'waiting_retry')""",
                (str(owner_id)[:200], run_id),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_run(run_id)

    def count_active_runs(self) -> int:
        """Count runs occupying the scheduler dispatch slot for this session."""
        with self._read():
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM durable_runs
                   WHERE session_id = ?
                     AND status IN ('dispatched', 'running')""",
                (self.session_id,),
            ).fetchone()
        return int(row["n"])

    def start_run(
        self, run_id: str, *, owner_id: str = "", now: float | None = None
    ) -> DurableRunRecord:
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                """UPDATE durable_runs
                   SET status = 'running', attempt = attempt + 1,
                       started_at = ?, ended_at = NULL
                   WHERE id = ? AND session_id = ? AND status = 'dispatched'
                     AND (? = '' OR owner_id = '' OR owner_id = ?)""",
                (now, run_id, self.session_id, str(owner_id)[:200], str(owner_id)[:200]),
            )
            if cursor.rowcount != 1:
                current = self.get_run(run_id)
                if current.status == "running":
                    return current
                raise AutonomyStoreError(
                    f"run {run_id} cannot start from {current.status}"
                )
        return self.get_run(run_id)

    def set_run_root_turn(self, run_id: str, root_turn_id: str) -> DurableRunRecord:
        self.get_run(run_id)
        with self._write():
            self._conn.execute(
                "UPDATE durable_runs SET root_turn_id = ? WHERE id = ?",
                (str(root_turn_id)[:180], run_id),
            )
        return self.get_run(run_id)

    def cancel_run(self, run_id: str, reason: str) -> DurableRunRecord:
        current = self.get_run(run_id)
        if current.terminal:
            return current
        now = time.time()
        reason = str(reason)[:1_000]
        with self._write():
            if current.status in {"queued", "dispatched", "waiting_retry"}:
                self._conn.execute(
                    """UPDATE durable_runs
                       SET status = 'cancelled', ended_at = ?, cancel_requested = 1,
                           cancel_reason = ? WHERE id = ?""",
                    (now, reason, run_id),
                )
            else:
                self._conn.execute(
                    """UPDATE durable_runs SET cancel_requested = 1,
                       cancel_reason = ? WHERE id = ?""",
                    (reason, run_id),
                )
        return self.get_run(run_id)

    def is_cancel_requested(self, run_id: str) -> bool:
        try:
            return self.get_run(run_id).cancel_requested
        except AutonomyStoreError:
            return True

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
        owner_id: str = "",
        now: float | None = None,
    ) -> DurableRunRecord:
        if status not in {"completed", "failed", "cancelled", "unknown"}:
            raise AutonomyStoreError(f"invalid terminal run status: {status}")
        now = time.time() if now is None else float(now)
        # Terminal classification and retry eligibility use the journal in the
        # same transaction. An outer worker must not turn an unknown effect
        # into an ordinary failed/completed Run.
        with self._write():
            current = self.get_run(run_id)
            if current.terminal:
                return current
            if current.status != "running":
                raise AutonomyStoreError(f"run {run_id} cannot finish from {current.status}")
            if owner_id and current.owner_id and current.owner_id != owner_id:
                raise AutonomyStoreError("run is owned by another host")
            automation = self.get_automation(current.automation_id)
            effects = self._conn.execute(
                "SELECT status FROM durable_tool_executions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            unresolved = any(row["status"] in {"started", "unknown"} for row in effects)
            if unresolved:
                status, result = "unknown", ""
                error = "tool side effect has no confirmed result; automatic retry is disabled"
                self._conn.execute(
                    """UPDATE durable_tool_executions
                       SET status = 'unknown', ended_at = ?, result_json = ?
                       WHERE run_id = ? AND status = 'started'""",
                    (now, _dump({"error": error}), run_id),
                )
            elif current.cancel_requested:
                status = "cancelled"
                error = error or current.cancel_reason
            # Without a per-tool idempotency contract, repeating a whole Run
            # after any tool executed can repeat already committed effects.
            should_retry = (
                status == "failed"
                and not any(row["status"] != "intended" for row in effects)
                and automation.recovery_policy == "retry"
                and current.attempt <= current.max_retries
            )
            if should_retry:
                self._conn.execute(
                    """UPDATE durable_runs
                       SET status = 'waiting_retry', scheduled_for = ?,
                           started_at = NULL, ended_at = NULL, result = '', error = ?
                       WHERE id = ?""",
                    (now + automation.retry_delay_seconds, str(error)[:4_000], run_id),
                )
            else:
                self._conn.execute(
                    """UPDATE durable_runs
                       SET status = ?, ended_at = ?, result = ?, error = ?
                       WHERE id = ?""",
                    (
                        status, now, str(result)[:8_000], str(error)[:4_000], run_id,
                    ),
                )
        return self.get_run(run_id)

    def recover_interrupted(
        self,
        *,
        active_run_ids: Iterable[str] = (),
        now: float | None = None,
    ) -> list[DurableRunRecord]:
        """Recover orphaned running rows without blindly replaying side effects."""
        now = time.time() if now is None else float(now)
        protected = set(active_run_ids)
        recovered: list[str] = []
        with self._write():
            dispatched = self._conn.execute(
                """SELECT id FROM durable_runs
                   WHERE session_id = ? AND status = 'dispatched'""",
                (self.session_id,),
            ).fetchall()
            for row in dispatched:
                run_id = str(row["id"])
                self._conn.execute(
                    """UPDATE durable_runs SET status = 'queued', error = ?
                       WHERE id = ?""",
                    (
                        "previous process stopped before this run started; safely requeued",
                        run_id,
                    ),
                )
                recovered.append(run_id)
            rows = self._conn.execute(
                """SELECT r.id, r.attempt, r.max_retries, a.recovery_policy,
                          a.retry_delay_seconds
                   FROM durable_runs r JOIN automations a ON a.id = r.automation_id
                   WHERE r.session_id = ? AND r.status = 'running'""",
                (self.session_id,),
            ).fetchall()
            for row in rows:
                run_id = str(row["id"])
                if run_id in protected:
                    continue
                started_effects = self._conn.execute(
                    """UPDATE durable_tool_executions
                       SET status = 'unknown', ended_at = ?, result_json = ?
                       WHERE run_id = ? AND status = 'started'""",
                    (
                        now,
                        _dump({"error": "host stopped after tool start; outcome unknown"}),
                        run_id,
                    ),
                ).rowcount
                can_retry = (
                    not started_effects
                    and
                    row["recovery_policy"] == "retry"
                    and int(row["attempt"]) <= int(row["max_retries"])
                )
                if can_retry:
                    self._conn.execute(
                        """UPDATE durable_runs
                           SET status = 'waiting_retry', scheduled_for = ?,
                               started_at = NULL, error = ? WHERE id = ?""",
                        (
                            now + float(row["retry_delay_seconds"]),
                            "previous process stopped while this run was active; retry scheduled",
                            run_id,
                        ),
                    )
                else:
                    self._conn.execute(
                        """UPDATE durable_runs
                           SET status = 'unknown', ended_at = ?, error = ?
                           WHERE id = ?""",
                        (
                            now,
                            "process restarted while this run was active; outcome is unknown and was not replayed",
                            run_id,
                        ),
                    )
                recovered.append(run_id)
        return [self.get_run(run_id) for run_id in recovered]

    def _insert_run_locked(
        self,
        automation: AutomationRecord,
        *,
        scheduled_for: float,
        trigger_payload: dict[str, Any],
        occurrence_key: str,
        now: float,
    ) -> str:
        run_id = f"run_{secrets.token_hex(7)}"
        cursor = self._conn.execute(
            """INSERT INTO durable_runs (
                id, automation_id, session_id, trigger_type,
                trigger_payload_json, status, attempt, max_retries,
                scheduled_for, created_at, occurrence_key, run_config_json
            ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""",
            (
                run_id, automation.id, self.session_id, automation.trigger.type,
                _dump(trigger_payload), automation.max_retries,
                scheduled_for, now, occurrence_key, _dump(automation.run_config),
            ),
        )
        if cursor.rowcount == 0:
            row = self._conn.execute(
                "SELECT id FROM durable_runs WHERE automation_id = ? AND occurrence_key = ?",
                (automation.id, occurrence_key),
            ).fetchone()
            assert row is not None
            return str(row["id"])
        return run_id

    def _run_query(self, predicate: str, parameters: tuple[Any, ...]):
        return self._conn.execute(
            f"""SELECT r.*, a.name AS automation_name, a.prompt AS prompt
                FROM durable_runs r JOIN automations a ON a.id = r.automation_id
                WHERE r.session_id = ? AND {predicate}
                ORDER BY r.created_at DESC""",
            (self.session_id, *parameters),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> DurableRunRecord:
        return DurableRunRecord(
            id=str(row["id"]),
            automation_id=str(row["automation_id"]),
            session_id=str(row["session_id"]),
            automation_name=str(row["automation_name"]),
            prompt=str(row["prompt"]),
            trigger_type=row["trigger_type"],
            trigger_payload=_load_object(row["trigger_payload_json"]),
            status=row["status"],
            attempt=int(row["attempt"]),
            max_retries=int(row["max_retries"]),
            scheduled_for=float(row["scheduled_for"]),
            created_at=float(row["created_at"]),
            started_at=(
                None if row["started_at"] is None else float(row["started_at"])
            ),
            ended_at=(
                None if row["ended_at"] is None else float(row["ended_at"])
            ),
            result=str(row["result"]),
            error=str(row["error"]),
            cancel_requested=bool(row["cancel_requested"]),
            cancel_reason=str(row["cancel_reason"]),
            root_turn_id=str(row["root_turn_id"]),
            occurrence_key=str(row["occurrence_key"]),
            owner_id=str(row["owner_id"]),
            run_config=_load_object(row["run_config_json"]),
        )
