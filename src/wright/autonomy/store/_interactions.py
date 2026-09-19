"""Permission and ask-user interaction records."""

from __future__ import annotations

import time
from typing import Any

from ._base import AutonomyStoreError, _StoreBase
from ._helpers import _dump, _load_object


class _InteractionsMixin(_StoreBase):
    def record_interaction(
        self, scope: str, request_id: str, *, kind: str, payload: dict[str, Any],
        run_id: str = "", now: float | None = None,
    ) -> None:
        if kind not in {"permission", "ask_user"}:
            raise AutonomyStoreError("unsupported interaction kind")
        now = time.time() if now is None else float(now)
        with self._write():
            self._conn.execute(
                """INSERT INTO pending_interactions
                   (scope, request_id, run_id, kind, payload_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(scope, request_id) DO NOTHING""",
                (scope, request_id, run_id[:300], kind, _dump(payload), now, now),
            )

    def resolve_interaction(
        self, scope: str, request_id: str, *, resolution: dict[str, Any],
        status: str = "resolved", now: float | None = None,
    ) -> bool:
        if status not in {"resolved", "cancelled"}:
            raise AutonomyStoreError("invalid interaction status")
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                """UPDATE pending_interactions SET status = ?, resolution_json = ?, updated_at = ?
                   WHERE scope = ? AND request_id = ? AND status = 'pending'""",
                (status, _dump(resolution), now, scope, request_id),
            )
            return cursor.rowcount == 1

    def terminate_pending_interactions(
        self, scope: str, *, reason: str, now: float | None = None
    ) -> int:
        """Explicitly end waits whose in-memory rendezvous died with a process."""
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                """UPDATE pending_interactions SET status = 'cancelled',
                   resolution_json = ?, updated_at = ? WHERE scope = ? AND status = 'pending'""",
                (_dump({"reason": reason[:1000]}), now, scope),
            )
            return cursor.rowcount

    def list_interactions(self, scope: str) -> list[dict[str, Any]]:
        with self._read():
            rows = self._conn.execute(
                "SELECT * FROM pending_interactions WHERE scope = ? ORDER BY created_at",
                (scope,),
            ).fetchall()
        return [
            {
                "request_id": str(row["request_id"]), "run_id": str(row["run_id"]),
                "kind": str(row["kind"]), "payload": _load_object(row["payload_json"]),
                "status": str(row["status"]), "resolution": _load_object(row["resolution_json"]),
            }
            for row in rows
        ]
