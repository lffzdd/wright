"""Idempotent interaction command operations for the autonomy store."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Any

from ._base import AutonomyNotFoundError, AutonomyStoreError, _StoreBase
from ._helpers import _bounded, _dump, _load_object


class _CommandsMixin(_StoreBase):
    _COMMAND_TERMINAL = frozenset({"completed", "failed", "cancelled", "unknown"})
    _COMMAND_STATUSES = frozenset({
        "accepted", "queued", "claimed", "running", "completed", "failed",
        "cancelled", "unknown",
    })

    def accept_command(
        self,
        scope: str,
        command_id: str,
        payload: dict[str, Any],
        *,
        now: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Durably accept a command before a caller reports it as accepted.

        The boolean is true only for the first submission. Repeating an id
        with changed content is rejected instead of creating an ambiguous
        second operation.
        """
        scope = _bounded(scope, "command scope", 300)
        command_id = _bounded(command_id, "command_id", 300)
        payload_json = _dump(payload)
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = time.time() if now is None else float(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM accepted_commands "
                "WHERE scope = ? AND command_id = ?",
                (scope, command_id),
            ).fetchone()
            if row is not None:
                if str(row["payload_hash"]) != digest:
                    raise AutonomyStoreError(
                        "command_id was already accepted with different content"
                    )
                return self._command_from_row(row), False
            self._conn.execute(
                """INSERT INTO accepted_commands
                   (scope, command_id, payload_json, payload_hash, status,
                    accepted_at, updated_at)
                   VALUES (?, ?, ?, ?, 'accepted', ?, ?)""",
                (scope, command_id, payload_json, digest, now, now),
            )
        return self.get_command(scope, command_id), True

    @staticmethod
    def _command_from_row(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "scope": str(row["scope"]),
            "command_id": str(row["command_id"]),
            "payload": _load_object(row["payload_json"]),
            "status": str(row["status"]),
            "result": _load_object(row["result_json"]),
            "run_id": str(row["run_id"]) if "run_id" in keys else "",
            "owner_id": str(row["owner_id"]) if "owner_id" in keys else "",
            "error": str(row["error"]) if "error" in keys else "",
            "cancel_reason": str(row["cancel_reason"]) if "cancel_reason" in keys else "",
            "accepted_at": float(row["accepted_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def queue_command(self, scope: str, command_id: str, *, now: float | None = None) -> dict[str, Any]:
        """Make an accepted command available to a worker without claiming it."""
        now = time.time() if now is None else float(now)
        with self._write():
            self._conn.execute(
                "UPDATE accepted_commands SET status = 'queued', updated_at = ? "
                "WHERE scope = ? AND command_id = ? AND status = 'accepted'",
                (now, scope, command_id),
            )
        return self.get_command(scope, command_id)

    def recover_commands(self, scope: str, *, now: float | None = None) -> list[dict[str, Any]]:
        """Recover work which has not started; classify live-process loss honestly.

        A process crash after ``running`` has no reliable answer about an
        external side effect, therefore that command is *not* automatically
        replayed.  ``accepted``, ``queued`` and ``claimed`` have not begun
        user work and can safely be offered to a fresh consumer.
        """
        now = time.time() if now is None else float(now)
        with self._write():
            self._conn.execute(
                "UPDATE accepted_commands SET status = 'queued', owner_id = '', updated_at = ? "
                "WHERE scope = ? AND status IN ('accepted', 'queued', 'claimed')",
                (now, scope),
            )
            self._conn.execute(
                "UPDATE accepted_commands SET status = 'unknown', error = "
                "'process stopped while command was running; outcome requires recovery', "
                "owner_id = '', updated_at = ? WHERE scope = ? AND status = 'running'",
                (now, scope),
            )
            rows = self._conn.execute(
                "SELECT * FROM accepted_commands WHERE scope = ? AND status = 'queued' "
                "ORDER BY accepted_at, command_id",
                (scope,),
            ).fetchall()
        return [self._command_from_row(row) for row in rows]

    def claim_command(
        self, scope: str, command_id: str, owner_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Atomically move a queued command to claimed for one consumer."""
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                "UPDATE accepted_commands SET status = 'claimed', owner_id = ?, updated_at = ? "
                "WHERE scope = ? AND command_id = ? AND status = 'queued'",
                (owner_id, now, scope, command_id),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM accepted_commands WHERE scope = ? AND command_id = ?",
                (scope, command_id),
            ).fetchone()
        return self._command_from_row(row)

    def start_command(
        self, scope: str, command_id: str, owner_id: str, *, run_id: str = "",
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                "UPDATE accepted_commands SET status = 'running', run_id = ?, updated_at = ? "
                "WHERE scope = ? AND command_id = ? AND status = 'claimed' AND owner_id = ?",
                (run_id, now, scope, command_id, owner_id),
            )
            return cursor.rowcount == 1

    def cancel_command(
        self, scope: str, command_id: str, *, reason: str, now: float | None = None
    ) -> dict[str, Any] | None:
        """Cancel work that has not started; a running command needs its owner."""
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                "UPDATE accepted_commands SET status = 'cancelled', cancel_reason = ?, "
                "updated_at = ? WHERE scope = ? AND command_id = ? "
                "AND status IN ('accepted', 'queued', 'claimed')",
                (reason[:1000], now, scope, command_id),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_command(scope, command_id)

    def complete_command(
        self,
        scope: str,
        command_id: str,
        result: dict[str, Any],
        *,
        status: str = "completed",
        now: float | None = None,
    ) -> dict[str, Any]:
        if status not in self._COMMAND_STATUSES:
            raise AutonomyStoreError(f"invalid command status: {status}")
        now = time.time() if now is None else float(now)
        run_id = str(result.get("run_id", ""))[:300]
        with self._write():
            cursor = self._conn.execute(
                """UPDATE accepted_commands SET status = ?, result_json = ?,
                   run_id = CASE WHEN ? != '' THEN ? ELSE run_id END, updated_at = ?
                   WHERE scope = ? AND command_id = ?
                   AND status NOT IN ('completed', 'failed', 'cancelled', 'unknown')""",
                (status, _dump(result), run_id, run_id, now, scope, command_id),
            )
            if cursor.rowcount != 1:
                raise AutonomyNotFoundError(f"unknown command_id: {command_id}")
        return self.get_command(scope, command_id)

    def get_command(self, scope: str, command_id: str) -> dict[str, Any]:
        with self._read():
            row = self._conn.execute(
                "SELECT * FROM accepted_commands WHERE scope = ? AND command_id = ?",
                (scope, command_id),
            ).fetchone()
        if row is None:
            raise AutonomyNotFoundError(f"unknown command_id: {command_id}")
        return self._command_from_row(row)
