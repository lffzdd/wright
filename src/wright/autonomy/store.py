"""SQLite source of truth for durable automations and concrete runs."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import AutomationRecord, DurableRunRecord, TriggerSpec


class AutonomyStoreError(ValueError):
    pass


class AutonomyNotFoundError(AutonomyStoreError):
    pass


class AutonomyStore:
    """Thread-safe, session-scoped view over a workspace-level SQLite DB."""

    # Version three turns accepted_commands into a recoverable command ledger.
    # It deliberately stores command payloads, ownership and the Run link in
    # SQLite instead of treating the in-process SessionService queue as truth.
    SCHEMA_VERSION = 4

    def __init__(self, path: Path, *, session_id: str, workspace_dir: Path) -> None:
        self.path = path.resolve()
        self.session_id = str(session_id)
        self.workspace_dir = workspace_dir.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._closed = False
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        with self._write():
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._initialize_schema()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise AutonomyStoreError("autonomy store is closed")

    @contextmanager
    def _read(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            yield

    @contextmanager
    def _write(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            with self._conn:
                yield

    def _initialize_schema(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, 1, 2, 3, self.SCHEMA_VERSION}:
            raise AutonomyStoreError(
                f"unsupported autonomy DB version: {version}"
            )
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS automations (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_json TEXT NOT NULL,
                trigger_state_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                recovery_policy TEXT NOT NULL,
                max_retries INTEGER NOT NULL,
                retry_delay_seconds REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                next_run_at REAL,
                last_run_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_automations_due
                ON automations(session_id, status, next_run_at);

            CREATE TABLE IF NOT EXISTS durable_runs (
                id TEXT PRIMARY KEY,
                automation_id TEXT NOT NULL REFERENCES automations(id),
                session_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 0,
                scheduled_for REAL NOT NULL,
                created_at REAL NOT NULL,
                started_at REAL,
                ended_at REAL,
                result TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                cancel_reason TEXT NOT NULL DEFAULT '',
                root_turn_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_durable_runs_dispatch
                ON durable_runs(session_id, status, scheduled_for);
            CREATE INDEX IF NOT EXISTS idx_durable_runs_automation
                ON durable_runs(automation_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS external_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                consumed_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_external_events_pending
                ON external_events(session_id, consumed_at, id);
        """)
        if version < 2:
            self._migrate_v2()
        if version < 3:
            self._migrate_v3()
        if version < 4:
            self._migrate_v4()
        self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def _migrate_v2(self) -> None:
        """Add recoverability metadata without rewriting historical rows."""
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(automations)").fetchall()
        }
        if "run_config_json" not in columns:
            self._conn.execute(
                "ALTER TABLE automations ADD COLUMN run_config_json TEXT NOT NULL DEFAULT '{}'"
            )
        run_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(durable_runs)").fetchall()
        }
        if "occurrence_key" not in run_columns:
            self._conn.execute(
                "ALTER TABLE durable_runs ADD COLUMN occurrence_key TEXT NOT NULL DEFAULT ''"
            )
        if "owner_id" not in run_columns:
            self._conn.execute(
                "ALTER TABLE durable_runs ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''"
            )
        if "run_config_json" not in run_columns:
            self._conn.execute(
                "ALTER TABLE durable_runs ADD COLUMN run_config_json TEXT NOT NULL DEFAULT '{}'"
            )
        self._conn.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_durable_runs_occurrence
                ON durable_runs(automation_id, occurrence_key)
                WHERE occurrence_key != '';
            CREATE TABLE IF NOT EXISTS accepted_commands (
                scope TEXT NOT NULL,
                command_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                accepted_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(scope, command_id)
            );
            CREATE TABLE IF NOT EXISTS durable_tool_executions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES durable_runs(id),
                step_id TEXT NOT NULL DEFAULT '',
                call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                effective_arguments_json TEXT NOT NULL,
                permission_json TEXT NOT NULL,
                environment_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                started_at REAL,
                ended_at REAL,
                UNIQUE(run_id, call_id)
            );
            CREATE INDEX IF NOT EXISTS idx_durable_tool_executions_run
                ON durable_tool_executions(run_id, created_at);
        """)

    def _migrate_v3(self) -> None:
        """Make command acceptance independently recoverable and claimable.

        Existing v2 rows have no proof that their queue item survived a
        process exit.  They remain ``accepted`` and are therefore safely
        eligible for a new owner to queue; rows which had started in a live
        process are only ever created by v3 and are recovered as ``unknown``.
        """
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(accepted_commands)").fetchall()
        }
        additions = {
            "run_id": "TEXT NOT NULL DEFAULT ''",
            "owner_id": "TEXT NOT NULL DEFAULT ''",
            "error": "TEXT NOT NULL DEFAULT ''",
            "cancel_reason": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE accepted_commands ADD COLUMN {name} {definition}"
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_accepted_commands_recovery "
            "ON accepted_commands(scope, status, updated_at)"
        )

    def _migrate_v4(self) -> None:
        """Persist interaction facts without serializing live Queue/Event objects."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS pending_interactions (
                scope TEXT NOT NULL,
                request_id TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                resolution_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(scope, request_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_interactions_status
                ON pending_interactions(scope, status, created_at);
        """)

    # -- Automation definitions -------------------------------------------------

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

    def list_automations(self) -> list[AutomationRecord]:
        with self._read():
            rows = self._conn.execute(
                """SELECT * FROM automations WHERE session_id = ?
                   ORDER BY created_at DESC""",
                (self.session_id,),
            ).fetchall()
        return [self._automation_from_row(row) for row in rows]

    def pause_automation(self, automation_id: str) -> AutomationRecord:
        """Stop materializing new runs; already queued runs may still execute.

        Pause only flips the definition to ``paused``, so ``claim_next_run``
        will still pick up existing queued/dispatched/waiting_retry rows.
        Use ``cancel_automation`` to abort those as well.
        """
        current = self.get_automation(automation_id)
        if current.status != "active":
            return current
        now = time.time()
        with self._write():
            self._conn.execute(
                """UPDATE automations SET status = 'paused', updated_at = ?
                   WHERE id = ? AND session_id = ?""",
                (now, automation_id, self.session_id),
            )
        return self.get_automation(automation_id)

    def resume_automation(
        self, automation_id: str, *, now: float | None = None
    ) -> AutomationRecord:
        current = self.get_automation(automation_id)
        if current.status == "active":
            return current
        if current.status != "paused":
            raise AutonomyStoreError(
                f"schedule {automation_id} cannot resume from {current.status}"
            )
        now = time.time() if now is None else float(now)
        next_run = self._resume_next_run(current.trigger, now)
        state = current.trigger_state
        if current.trigger.type == "file_change":
            state = self._file_snapshot(current.trigger.path)
        elif current.trigger.type == "web_change":
            state = {}
        with self._write():
            self._conn.execute(
                """UPDATE automations
                   SET status = 'active', updated_at = ?, next_run_at = ?,
                       trigger_state_json = ?
                   WHERE id = ? AND session_id = ?""",
                (now, next_run, _dump(state), automation_id, self.session_id),
            )
        return self.get_automation(automation_id)

    def cancel_automation(self, automation_id: str, reason: str) -> AutomationRecord:
        self.get_automation(automation_id)
        now = time.time()
        reason = str(reason)[:1_000]
        with self._write():
            self._conn.execute(
                """UPDATE automations
                   SET status = 'cancelled', updated_at = ?, next_run_at = NULL
                   WHERE id = ? AND session_id = ?""",
                (now, automation_id, self.session_id),
            )
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
        return self.get_automation(automation_id)

    # -- Trigger materialization ------------------------------------------------

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

    # -- Recoverable command acceptance ---------------------------------------

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

    _COMMAND_TERMINAL = frozenset({"completed", "failed", "cancelled", "unknown"})
    _COMMAND_STATUSES = frozenset({
        "accepted", "queued", "claimed", "running", "completed", "failed",
        "cancelled", "unknown",
    })

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

    def attach_command_run(
        self, scope: str, command_id: str, owner_id: str, run_id: str,
        *, now: float | None = None,
    ) -> bool:
        """Persist the Run identity before its first model/tool side effect."""
        if not run_id:
            raise AutonomyStoreError("run_id is required")
        now = time.time() if now is None else float(now)
        with self._write():
            cursor = self._conn.execute(
                "UPDATE accepted_commands SET run_id = ?, updated_at = ? "
                "WHERE scope = ? AND command_id = ? AND status = 'running' AND owner_id = ?",
                (run_id[:300], now, scope, command_id, owner_id),
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

    # -- Persisted interaction facts ------------------------------------------

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

    # -- Tool side-effect journal ----------------------------------------------

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
        now: float | None = None,
    ) -> None:
        """Persist intent before a durable tool is allowed to run."""
        now = time.time() if now is None else float(now)
        execution_id = f"tool_{run_id}_{call_id}"
        with self._write():
            self._conn.execute(
                """INSERT INTO durable_tool_executions
                   (id, run_id, step_id, call_id, tool_name,
                    effective_arguments_json, permission_json, environment_json,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'intended', ?)
                   ON CONFLICT(run_id, call_id) DO NOTHING""",
                (
                    execution_id, run_id, step_id[:200], call_id[:300], tool_name[:200],
                    _dump(_redact_arguments(arguments)), _dump(permission),
                    _dump(environment), now,
                ),
            )

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
                "call_id": str(row["call_id"]), "tool_name": str(row["tool_name"]),
                "status": str(row["status"]),
                "arguments": _load_object(row["effective_arguments_json"]),
                "result": _load_object(row["result_json"]),
            }
            for row in rows
        ]

    def materialize_due(self, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else float(now)
        created: list[str] = []
        with self._write():
            rows = self._conn.execute(
                """SELECT * FROM automations
                   WHERE session_id = ? AND status = 'active'
                   ORDER BY created_at""",
                (self.session_id,),
            ).fetchall()
            for row in rows:
                automation = self._automation_from_row(row)
                trigger = automation.trigger
                if trigger.type in {"once", "interval"}:
                    due = automation.next_run_at
                    if due is None or due > now:
                        continue
                    created_run = False
                    if not self._has_live_run_locked(automation.id):
                        created.append(self._insert_run_locked(
                            automation,
                            scheduled_for=due,
                            trigger_payload={"scheduled_for": due},
                            occurrence_key=f"{trigger.type}:{due:.6f}",
                            now=now,
                        ))
                        created_run = True
                    if trigger.type == "once":
                        self._conn.execute(
                            """UPDATE automations
                               SET status = 'completed', next_run_at = NULL,
                                   last_run_at = ?, updated_at = ? WHERE id = ?""",
                            (now, now, automation.id),
                        )
                    else:
                        next_run = self._advance_interval(
                            due, float(trigger.every_seconds), now
                        )
                        self._conn.execute(
                            """UPDATE automations SET next_run_at = ?,
                               updated_at = ?,
                               last_run_at = CASE WHEN ? THEN ? ELSE last_run_at END
                               WHERE id = ?""",
                            (
                                next_run, now, int(created_run), now,
                                automation.id,
                            ),
                        )
                elif trigger.type == "file_change":
                    snapshot = self._file_snapshot(trigger.path)
                    if snapshot == automation.trigger_state:
                        continue
                    # Coalesce, but do not lose, a change while the previous
                    # run is live. Keeping the old baseline makes the next poll
                    # materialize one follow-up after that run terminates.
                    if self._has_live_run_locked(automation.id):
                        continue
                    self._conn.execute(
                        """UPDATE automations SET trigger_state_json = ?,
                           updated_at = ?, last_run_at = ? WHERE id = ?""",
                        (_dump(snapshot), now, now, automation.id),
                    )
                    created.append(self._insert_run_locked(
                        automation,
                        scheduled_for=now,
                        trigger_payload={
                            "path": trigger.path,
                            "before": automation.trigger_state,
                            "after": snapshot,
                        },
                        occurrence_key=f"file:{snapshot.get('mtime_ns')}:{snapshot.get('size')}",
                        now=now,
                    ))

            events = self._conn.execute(
                """SELECT * FROM external_events
                   WHERE session_id = ? AND consumed_at IS NULL
                   ORDER BY id LIMIT 100""",
                (self.session_id,),
            ).fetchall()
            for event in events:
                matching = self._conn.execute(
                    """SELECT * FROM automations
                       WHERE session_id = ? AND status = 'active'
                         AND trigger_type = 'event'""",
                    (self.session_id,),
                ).fetchall()
                for row in matching:
                    automation = self._automation_from_row(row)
                    if automation.trigger.event_name != event["name"]:
                        continue
                    payload = {
                        "event_id": int(event["id"]),
                        "event_name": str(event["name"]),
                        "payload": _load_object(event["payload_json"]),
                    }
                    # Coalesce, but do not lose, events while a previous run
                    # is live. The event is consumed so it cannot be replayed
                    # from the log; pending_event holds the merged follow-up.
                    if self._has_live_run_locked(automation.id):
                        self._note_pending_event_locked(
                            automation, payload, now
                        )
                        continue
                    created.append(self._insert_run_locked(
                        automation,
                        scheduled_for=float(event["created_at"]),
                        trigger_payload=payload,
                        occurrence_key=f"event:{int(event['id'])}",
                        now=now,
                    ))
                    self._conn.execute(
                        """UPDATE automations SET last_run_at = ?, updated_at = ?
                           WHERE id = ?""",
                        (now, now, automation.id),
                    )
                self._conn.execute(
                    "UPDATE external_events SET consumed_at = ? WHERE id = ?",
                    (now, int(event["id"])),
                )
            self._flush_pending_events_locked(created, now)
        return created

    def list_due_web_probes(
        self, *, now: float | None = None
    ) -> list[AutomationRecord]:
        """Read due probes; network work intentionally happens outside DB locks."""
        now = time.time() if now is None else float(now)
        with self._read():
            rows = self._conn.execute(
                """SELECT * FROM automations
                   WHERE session_id = ? AND status = 'active'
                     AND trigger_type = 'web_change'
                     AND next_run_at <= ?
                   ORDER BY next_run_at LIMIT 20""",
                (self.session_id, now),
            ).fetchall()
        return [self._automation_from_row(row) for row in rows]

    def record_web_probe(
        self,
        automation_id: str,
        snapshot: dict[str, Any],
        *,
        now: float | None = None,
    ) -> str | None:
        snapshot_json = _dump(snapshot)
        if len(snapshot_json) > 20_000:
            raise AutonomyStoreError("web probe snapshot exceeds 20000 chars")
        now = time.time() if now is None else float(now)
        with self._write():
            row = self._conn.execute(
                """SELECT * FROM automations
                   WHERE id = ? AND session_id = ? AND status = 'active'
                     AND trigger_type = 'web_change'""",
                (automation_id, self.session_id),
            ).fetchone()
            if row is None:
                return None
            automation = self._automation_from_row(row)
            previous = automation.trigger_state.get("snapshot")
            next_run = now + float(automation.trigger.every_seconds or 0)
            changed = previous is not None and previous != snapshot
            live = self._has_live_run_locked(automation.id)
            # As with file changes, preserve the old baseline while a previous
            # run is live so one follow-up change is eventually delivered.
            effective_snapshot = previous if changed and live else snapshot
            state = {"snapshot": effective_snapshot, "last_error": ""}
            run_id: str | None = None
            if changed and not live:
                run_id = self._insert_run_locked(
                    automation,
                    scheduled_for=now,
                    trigger_payload={
                        "url": automation.trigger.url,
                        "before": previous,
                        "after": snapshot,
                    },
                    occurrence_key=f"web:{_hash_payload(snapshot)}",
                    now=now,
                )
            self._conn.execute(
                """UPDATE automations
                   SET trigger_state_json = ?, next_run_at = ?, updated_at = ?,
                       last_run_at = CASE WHEN ? THEN ? ELSE last_run_at END
                   WHERE id = ?""",
                (_dump(state), next_run, now, int(run_id is not None), now, automation.id),
            )
            return run_id

    def defer_web_probe(
        self,
        automation_id: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else float(now)
        with self._write():
            row = self._conn.execute(
                """SELECT * FROM automations
                   WHERE id = ? AND session_id = ? AND status = 'active'
                     AND trigger_type = 'web_change'""",
                (automation_id, self.session_id),
            ).fetchone()
            if row is None:
                return
            automation = self._automation_from_row(row)
            state = dict(automation.trigger_state)
            state["last_error"] = str(error)[:1_000]
            next_run = now + float(automation.trigger.every_seconds or 0)
            self._conn.execute(
                """UPDATE automations SET trigger_state_json = ?,
                   next_run_at = ?, updated_at = ? WHERE id = ?""",
                (_dump(state), next_run, now, automation.id),
            )

    # -- Concrete durable runs --------------------------------------------------

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

    # -- Internal conversion/helpers -------------------------------------------

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


def _bounded(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise AutonomyStoreError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise AutonomyStoreError(f"{name} must be non-empty")
    if len(cleaned) > limit:
        raise AutonomyStoreError(f"{name} exceeds {limit} chars")
    return cleaned


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AutonomyStoreError(f"value is not JSON serializable: {exc}") from exc


def _load_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AutonomyStoreError(f"corrupt JSON in autonomy DB: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AutonomyStoreError("autonomy DB JSON value must be an object")
    return parsed


def _hash_payload(value: dict[str, Any]) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()[:32]


def _safe_run_config(value: dict[str, Any]) -> dict[str, Any]:
    """Persist configuration snapshots, never credential values.

    The present API accepts only the small declarative durable-run surface.
    Unknown nesting is rejected so a caller cannot accidentally copy an
    environment or API key into the task database.
    """
    if not isinstance(value, dict):
        raise AutonomyStoreError("run_config must be an object")
    allowed = {"profile", "model", "transport", "environment", "max_steps"}
    unknown = set(value) - allowed
    if unknown:
        raise AutonomyStoreError(f"unsupported run_config fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if key == "max_steps":
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 1_000:
                raise AutonomyStoreError("run_config.max_steps must be between 1 and 1000")
            clean[key] = item
        elif key in {"profile", "model", "transport", "environment"}:
            clean[key] = _bounded(str(item), f"run_config.{key}", 200)
    if "profile" in clean and clean["profile"] != "durable":
        raise AutonomyStoreError("durable automations only support the durable profile")
    if "transport" in clean and clean["transport"] not in {"auto", "chat", "responses"}:
        raise AutonomyStoreError("run_config.transport must be auto, chat, or responses")
    if "environment" in clean and clean["environment"] != "local":
        raise AutonomyStoreError("durable automations currently require the local environment")
    return clean


def _redact_arguments(value: dict[str, Any]) -> dict[str, Any]:
    sensitive = {"api_key", "authorization", "token", "password", "secret"}
    return {
        str(key): "[redacted]" if str(key).casefold() in sensitive else item
        for key, item in value.items()
    }
