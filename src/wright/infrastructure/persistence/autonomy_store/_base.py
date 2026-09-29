"""Connection and transaction ownership for the shared task store.

Scheduling errors live with the scheduling ports. This store also raises them
for commands, interactions, and history so one SQLite transaction can fail
closed without a second error hierarchy.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ....application.scheduling.contracts import JobNotFoundError, SchedulingError


class AutonomyStoreError(SchedulingError):
    """Shared-store failure. Scheduling callers catch :class:`SchedulingError`."""


class AutonomyNotFoundError(JobNotFoundError, AutonomyStoreError):
    """Missing row. Scheduling callers catch :class:`JobNotFoundError`."""


class _StoreBase:
    # Version three turns accepted_commands into a recoverable command ledger.
    # It deliberately stores command payloads, ownership and the Run link in
    # SQLite instead of treating the in-process SessionService queue as truth.
    SCHEMA_VERSION = 6

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
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._write():
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
        """Take the SQLite reserved lock before the first read in the transaction."""
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
