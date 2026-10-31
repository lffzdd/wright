"""Serialize review mutations across clients and directory workers."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...core.paths import project_state_dir
from ...infrastructure.file_lock import FileLock, FileLockUnavailable
from ..execution.directory import DirectoryWaitCancelled


class ChangeBusyError(ValueError):
    pass


@contextmanager
def workspace_change_lock(root: Path):
    directory = project_state_dir(root.resolve())
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(directory / "changes.lock", blocking=False):
            yield
    except FileLockUnavailable as exc:
        raise ChangeBusyError(
            "another change operation is in progress; refresh and retry"
        ) from exc


@contextmanager
def change_operation(service: Any):
    """Reserve the same execution root used by turns and background work.

    A pre-cancelled wait token grants an idle directory immediately and rejects
    an occupied directory without queuing a stale UI operation behind a turn.
    The caller owns the file lock, so direct journal and Git mutations use it
    exactly once.
    """
    if service.summary().get("execution") != "idle":
        raise ChangeBusyError("a turn is executing; wait until the session is idle")
    runtime = service.runtime
    root = Path(runtime.session_state.workspace_dir)
    coordinator = getattr(runtime, "directory_coordinator", None)
    lease = None
    if coordinator is not None:
        cancel = threading.Event()
        cancel.set()
        try:
            lease = coordinator.acquire(
                root,
                kind="review",
                holder_id=uuid4().hex,
                session_id=service.session_id,
                label="review changes",
                cancel=cancel,
            )
        except DirectoryWaitCancelled as exc:
            raise ChangeBusyError(
                "the execution directory is busy; refresh when the task finishes"
            ) from exc
    try:
        # A turn may have been queued between the first status read and lease.
        if service.summary().get("execution") != "idle":
            raise ChangeBusyError("a turn is executing; wait until the session is idle")
        yield
    finally:
        if lease is not None:
            lease.release()
