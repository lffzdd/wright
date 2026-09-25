"""Schema initialization and migrations for the autonomy store."""

from __future__ import annotations

from ._base import AutonomyStoreError, _StoreBase


class _MigrationsMixin(_StoreBase):
    def _initialize_schema(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, 1, 2, 3, 4, 5, self.SCHEMA_VERSION}:
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
        if version < 5:
            self._migrate_v5()
        if version < 6:
            self._migrate_v6()
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
                agent_task_id TEXT NOT NULL DEFAULT '',
                session_run_id TEXT NOT NULL DEFAULT '',
                parent_agent_task_id TEXT NOT NULL DEFAULT '',
                call_id TEXT NOT NULL,
                provider_call_id TEXT NOT NULL DEFAULT '',
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
            CREATE TABLE IF NOT EXISTS durable_run_history (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES durable_runs(id),
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_key TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(run_id, sequence),
                UNIQUE(run_id, event_type, event_key)
            );
            CREATE INDEX IF NOT EXISTS idx_durable_run_history_run
                ON durable_run_history(run_id, sequence);
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

    def _migrate_v5(self) -> None:
        """Add the durable Run history and parent/child execution identity."""
        columns = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(durable_tool_executions)"
            ).fetchall()
        }
        for name, definition in {
            "agent_task_id": "TEXT NOT NULL DEFAULT ''",
            "session_run_id": "TEXT NOT NULL DEFAULT ''",
            "parent_agent_task_id": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE durable_tool_executions ADD COLUMN {name} {definition}"
                )
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS durable_run_history (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES durable_runs(id),
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_key TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(run_id, sequence),
                UNIQUE(run_id, event_type, event_key)
            );
            CREATE INDEX IF NOT EXISTS idx_durable_run_history_run
                ON durable_run_history(run_id, sequence);
        """)

    def _migrate_v6(self) -> None:
        """Keep provider call IDs while scoping persisted execution keys."""
        columns = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(durable_tool_executions)"
            ).fetchall()
        }
        if "provider_call_id" not in columns:
            self._conn.execute(
                "ALTER TABLE durable_tool_executions "
                "ADD COLUMN provider_call_id TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute(
            "UPDATE durable_tool_executions SET provider_call_id = call_id "
            "WHERE provider_call_id = ''"
        )
