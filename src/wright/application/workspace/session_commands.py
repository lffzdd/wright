"""Session-scoped workspace changes.

Routes authenticate and pass the session service. Idle checks, mutation and
checkpointing for grants live here rather than in the HTTP handler.
"""

from __future__ import annotations

from typing import Any

from ..session.errors import SessionServiceError
from ..tool_execution.permissions import PermissionConflict
from .documents import DocumentError
from .grants import GrantError, list_grants, resource_change
from .journal import ChangeJournalError, SessionChangeJournal
from .memory_view import bound_project


def require_idle(service: Any) -> None:
    if service.summary().get("execution") != "idle":
        raise SessionServiceError(
            "a turn is still executing; change this when the session is idle"
        )


def _permissions(service):
    return service.runtime.agent.executor.permissions


def session_grants(service: Any) -> dict[str, Any]:
    control = getattr(service.runtime, "sandbox_control", None)
    return list_grants(service.runtime.session_state, _permissions(service), control.status() if control else {})


def add_grant(service: Any, resource: dict[str, Any], *, lifetime: str, expected_version: str) -> dict[str, Any]:
    permissions = _permissions(service)
    session = service.runtime.session_state
    try:
        with permissions.repository.transaction():
            permissions.validate(session, expected_version)
            snapshot = permissions.snapshot(session)
            change = resource_change(session, service.runtime.agent.executor.backend, snapshot, resource, lifetime)
            permissions.commit(change, session, lambda _: service.persist(), expected_version=expected_version)
    except PermissionConflict:
        raise
    except (GrantError, TypeError, ValueError, OSError) as exc:
        raise SessionServiceError(str(exc)) from exc
    service.publisher.publish("session.policy_updated", {})
    return session_grants(service)


def revoke_grant(service: Any, rule_id: str, *, source: str, expected_version: str) -> dict[str, Any]:
    permissions = _permissions(service)
    session = service.runtime.session_state
    try:
        permissions.revoke(session, rule_id, source, expected_version, lambda _: service.persist())
    except PermissionConflict:
        raise
    except (ValueError, OSError) as exc:
        raise SessionServiceError(str(exc)) from exc
    interactions = getattr(service, "interactions", None)
    if interactions is not None:
        for request in interactions.snapshot():
            if request.get("kind") == "permission":
                interactions.resolve(request["request_id"], "deny")
    service.publisher.publish("session.policy_updated", {})
    return session_grants(service)


def sandbox_action(service: Any, action: str) -> dict[str, Any]:
    control = getattr(service.runtime, "sandbox_control", None)
    if control is None or action not in {"setup", "cleanup"}:
        raise SessionServiceError("Sandbox setup is unavailable")
    try:
        return control.initialize(cleanup=action == "cleanup")
    except RuntimeError as error:
        raise SessionServiceError(str(error)) from error


def review_action(service: Any, action: str, paths: list[str], *, confirm: bool) -> dict[str, Any]:
    journal = SessionChangeJournal(service.runtime.session_state)
    try:
        if action == "accept":
            return journal.accept(paths, confirm=confirm)
        if action == "revert":
            return journal.revert(paths, confirm=confirm)
    except ChangeJournalError as exc:
        raise SessionServiceError(str(exc)) from exc
    raise SessionServiceError("review action must be accept or revert")


def memory_binding(service: Any) -> tuple[Any, str]:
    agent = getattr(service.runtime, "agent", None)
    memory = getattr(agent, "memory", None)
    found = getattr(memory, "service", None)
    if found is None:
        raise SessionServiceError("memory service is unavailable")
    return found, bound_project(service.runtime.session_state)


def store_session_document(service: Any, filename: str, data: bytes) -> dict[str, Any]:
    try:
        return service.add_document(service.session_id, filename, data)
    except DocumentError as exc:
        raise SessionServiceError(str(exc)) from exc


__all__ = [
    "add_grant",
    "memory_binding",
    "require_idle",
    "review_action",
    "revoke_grant",
    "sandbox_action",
    "session_grants",
    "store_session_document",
]
