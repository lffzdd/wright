"""Commit an authorization change across session state and permission config.

The commit point is this function. Persistent config is replaced first. Session
memory and the session checkpoint change only after that replace returns. If
the checkpoint fails, session memory is restored and only this commit's
persistent entries are removed under the config file lock. A later tool
failure is not an authorization rollback.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ...domain.model.session.session import Session
from ...domain.policy.permission.types import AuthorizationChange
from ...infrastructure.config.permission_store import (
    apply_persistent_authorization,
    revert_persistent_authorization,
)


def commit_authorization(
    change: AuthorizationChange,
    *,
    session: Session,
    save_checkpoint: Callable[[Session], object] | None = None,
) -> None:
    if change == AuthorizationChange():
        return
    persistent = bool(change.persistent_rules or change.persistent_directories)
    if persistent:
        apply_persistent_authorization(change)
    directories = session.working_directories_snapshot()
    rules = [dict(rule) for rule in session.permission_rules]
    try:
        for directory in change.session_directories:
            session.add_working_directory(Path(directory.value))
        for rule in change.session_rules:
            if not isinstance(rule, dict):
                raise TypeError("session authorization rules must be structured records")
            session.add_permission_rule(rule)
        if save_checkpoint is not None and (
            change.session_directories or change.session_rules
        ):
            save_checkpoint(session)
    except Exception:
        session.restore_authorization(directories, rules)
        if persistent:
            revert_persistent_authorization(change)
        raise
