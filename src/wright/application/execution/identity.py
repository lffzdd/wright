"""Bind cross-type identifier checks to the records each owner keeps."""

from __future__ import annotations

from ...domain.gateway.scheduling import JobNotFoundError
from ...domain.model.agent import AgentControlError
from ...domain.model.execution import (
    ExecutionIdentity,
    ExecutionKind,
    ExecutionKindMismatch,
    ExecutionNotFound,
    ExecutionWaitCancelled,
)


def bind_identity(session, store=None) -> ExecutionIdentity:
    """Build probes from the records each owner already keeps."""

    def has_agent(identifier: str) -> bool:
        try:
            session.control_plane.get(identifier)
        except AgentControlError:
            return False
        return True

    def has_run(identifier: str) -> bool:
        if store is None:
            return False
        try:
            store.get_run(identifier)
        except JobNotFoundError:
            return False
        return True

    def has_schedule(identifier: str) -> bool:
        if store is None:
            return False
        try:
            store.get_job(identifier)
        except JobNotFoundError:
            return False
        return True

    return ExecutionIdentity(
        has_agent=has_agent,
        has_command=lambda identifier: session.get_command(identifier) is not None,
        has_run=has_run,
        has_schedule=has_schedule,
    )


__all__ = [
    "ExecutionIdentity",
    "ExecutionKind",
    "ExecutionKindMismatch",
    "ExecutionNotFound",
    "ExecutionWaitCancelled",
    "bind_identity",
]
