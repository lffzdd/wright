"""Durable run history facts."""

from __future__ import annotations

import time
from typing import Any

from ._base import AutonomyNotFoundError, _StoreBase
from ._helpers import _bounded, _bounded_history_payload, _dump, _load_object


class _HistoryMixin(_StoreBase):
    def record_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        event_key: str = "",
        now: float | None = None,
    ) -> str:
        """Append one bounded, queryable fact to a durable Run's history."""
        event_type = _bounded(event_type, "event_type", 120)
        event_key = str(event_key)[:300]
        now = time.time() if now is None else float(now)
        value = _bounded_history_payload(payload or {})
        with self._write():
            row = self._conn.execute(
                "SELECT session_id FROM durable_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None or str(row["session_id"]) != self.session_id:
                raise AutonomyNotFoundError(f"Unknown task_id: {run_id}")
            if event_key:
                existing = self._conn.execute(
                    "SELECT event_id FROM durable_run_history "
                    "WHERE run_id = ? AND event_type = ? AND event_key = ?",
                    (run_id, event_type, event_key),
                ).fetchone()
                if existing is not None:
                    return str(existing["event_id"])
            sequence = int(self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                "FROM durable_run_history WHERE run_id = ?",
                (run_id,),
            ).fetchone()["next_sequence"])
            event_id = f"{run_id}:event:{sequence}"
            self._conn.execute(
                """INSERT INTO durable_run_history
                   (event_id, run_id, sequence, event_type, event_key,
                    payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_id, run_id, sequence, event_type, event_key, _dump(value), now),
            )
            return event_id

    def list_run_history(self, run_id: str) -> list[dict[str, Any]]:
        with self._read():
            rows = self._conn.execute(
                """SELECT event_id, run_id, sequence, event_type, event_key,
                          payload_json, created_at
                   FROM durable_run_history WHERE run_id = ? ORDER BY sequence""",
                (run_id,),
            ).fetchall()
        return [
            {
                "event_id": str(row["event_id"]),
                "run_id": str(row["run_id"]),
                "sequence": int(row["sequence"]),
                "event_type": str(row["event_type"]),
                "event_key": str(row["event_key"]),
                "payload": _load_object(row["payload_json"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def run_history(self, run_id: str) -> dict[str, Any]:
        """Return the authoritative durable Run projection for adapters."""
        # This is a project-level query used after the source Session has been
        # closed.  The database path is already scoped by the ApplicationHost;
        # unlike session commands it must not require the vanished source
        # session_id.
        with self._read():
            row = self._conn.execute(
                """SELECT r.*, a.name AS automation_name, a.prompt AS prompt
                   FROM durable_runs r JOIN automations a ON a.id = r.automation_id
                   WHERE r.id = ?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise AutonomyNotFoundError(f"Unknown task_id: {run_id}")
        run = self._run_from_row(row)
        return {
            "run": run.to_dict(),
            "history": self.list_run_history(run_id),
            "tool_executions": self.list_tool_executions(run_id),
        }
