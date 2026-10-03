"""Thin application entry point; PermissionService owns the whole transaction."""
from __future__ import annotations

from collections.abc import Callable

from ...domain.model.session.session import Session
from ...domain.policy.permission.types import AuthorizationChange
from .permissions import PermissionService


def commit_authorization(change: AuthorizationChange, *, session: Session,
                         permissions: PermissionService,
                         save_checkpoint: Callable[[Session], object] | None = None) -> None:
    permissions.commit(change, session, save_checkpoint)
