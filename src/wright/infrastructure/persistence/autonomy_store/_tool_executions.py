"""Durable tool intent, result, and recovery records."""

from __future__ import annotations

import secrets
import time
from typing import Any

from ._base import AutonomyStoreError, _StoreBase
from ._helpers import _dump, _load_object, _redact_arguments


class _ToolExecutionsMixin(_StoreBase):
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
        now: float | None = None,
    ) -> str:
        """Persist intent before a durable tool is allowed to run."""
        now = time.time() if now is None else float(now)
        provider_call_id = str(call_id)[:300]
        with self._write():
            existing = self._conn.execute(
                """SELECT call_id FROM durable_tool_executions
                   WHERE run_id = ? AND provider_call_id = ?
                     AND agent_task_id = ? AND session_run_id = ?
                     AND parent_agent_task_id = ?""",
                (
                    run_id, provider_call_id, agent_task_id[:200],
                    session_run_id[:300], parent_agent_task_id[:200],
                ),
            ).fetchone()
            if existing is not None:
                return str(existing["call_id"])

            stored_call_id = provider_call_id
            collision = self._conn.execute(
                "SELECT 1 FROM durable_tool_executions WHERE run_id = ? AND call_id = ?",
                (run_id, stored_call_id),
            ).fetchone()
            if collision is not None:
                scope = ":".join(
                    item for item in (
                        agent_task_id, session_run_id, step_id, provider_call_id
                    ) if item
                )[:260]
                stored_call_id = scope or f"scoped:{provider_call_id}"
                while self._conn.execute(
                    "SELECT 1 FROM durable_tool_executions WHERE run_id = ? AND call_id = ?",
                    (run_id, stored_call_id),
                ).fetchone() is not None:
                    stored_call_id = f"{stored_call_id[:285]}:{secrets.token_hex(6)}"
            execution_id = f"tool_{run_id}_{stored_call_id}"
            self._conn.execute(
                """INSERT INTO durable_tool_executions
                   (id, run_id, step_id, agent_task_id, session_run_id,
                    parent_agent_task_id, call_id, provider_call_id, tool_name,
                    effective_arguments_json, permission_json, environment_json,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intended', ?)
                   ON CONFLICT(run_id, call_id) DO NOTHING""",
                (
                    execution_id, run_id, step_id[:200], agent_task_id[:200],
                    session_run_id[:300], parent_agent_task_id[:200], stored_call_id,
                    provider_call_id, tool_name[:200],
                    _dump(_redact_arguments(arguments)), _dump(permission),
                    _dump(environment), now,
                ),
            )
            return stored_call_id

    def mark_tool_started(
        self, run_id: str, call_id: str, *, now: float | None = None
    ) -> None:
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                """UPDATE durable_tool_executions SET status = 'started', started_at = ?
                   WHERE run_id = ? AND call_id = ? AND status = 'intended'""",
                (now, run_id, call_id),
            )
            if cursor.rowcount != 1:
                raise AutonomyStoreError("tool intent is missing or already terminal")

    def record_tool_result(
        self,
        run_id: str,
        call_id: str,
        result: dict[str, Any],
        *,
        status: str,
        now: float | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "unknown"}:
            raise AutonomyStoreError(f"invalid tool execution status: {status}")
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                """UPDATE durable_tool_executions
                   SET status = ?, result_json = ?, ended_at = ?
                   WHERE run_id = ? AND call_id = ? AND status = 'started'""",
                (status, _dump(result), now, run_id, call_id),
            )
            if cursor.rowcount != 1:
                raise AutonomyStoreError("tool result has no started intent")

    def recover_tool_executions(self, run_id: str, *, now: float | None = None) -> int:
        """Mark started effects unknown; recovery never repeats them implicitly."""
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                """UPDATE durable_tool_executions
                   SET status = 'unknown', ended_at = ?,
                       result_json = ?
                   WHERE run_id = ? AND status = 'started'""",
                (now, _dump({"error": "host stopped after tool start; outcome unknown"}), run_id),
            )
        return cursor.rowcount

    def list_tool_executions(self, run_id: str) -> list[dict[str, Any]]:
        with self._read():
            rows = self._conn.execute(
                "SELECT * FROM durable_tool_executions WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [
            {
                "id": str(row["id"]), "run_id": str(row["run_id"]),
                "call_id": str(row["provider_call_id"] or row["call_id"]),
                "provider_call_id": str(row["provider_call_id"] or row["call_id"]),
                "execution_call_id": str(row["call_id"]),
                "tool_name": str(row["tool_name"]),
                "step_id": str(row["step_id"]),
                "agent_task_id": str(row["agent_task_id"]),
                "session_run_id": str(row["session_run_id"]),
                "parent_agent_task_id": str(row["parent_agent_task_id"]),
                "status": str(row["status"]),
                "arguments": _load_object(row["effective_arguments_json"]),
                "result": _load_object(row["result_json"]),
            }
            for row in rows
        ]
