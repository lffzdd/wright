"""Session-scoped workspace changes.

Routes authenticate and pass the session service. Idle checks, mutation and
checkpointing for grants live here rather than in the HTTP handler.
"""

from __future__ import annotations

from typing import Any

from ..session.errors import SessionServiceError
from .documents import DocumentError
from .grants import (
    GrantError,
    add_session_rule,
    change_directory,
    list_grants,
    revoke_persistent_rule,
    revoke_session_rule,
)
from .journal import ChangeJournalError, SessionChangeJournal
from .memory_view import bound_project


def require_idle(service: Any) -> None:
    if service.summary().get("execution") != "idle":
        raise SessionServiceError(
            "a turn is still executing; change this when the session is idle"
        )


def session_grants(service: Any) -> dict[str, Any]:
    runtime = service.runtime
    return list_grants(runtime.session_state, runtime.permission_settings)


def add_grant(service: Any, rule: dict[str, Any]) -> dict[str, Any]:
    require_idle(service)
    try:
        created = add_session_rule(service.runtime.session_state, rule)
    except (GrantError, TypeError, ValueError) as exc:
        raise SessionServiceError(str(exc)) from exc
    service.persist()
    service.publisher.publish("session.policy_updated", {})
    return created


def revoke_grant(service: Any, rule_id: str, *, confirm: bool) -> dict[str, Any]:
    require_idle(service)
    runtime = service.runtime
    try:
        try:
            result = revoke_session_rule(runtime.session_state, rule_id, confirm=confirm)
        except GrantError:
            result = revoke_persistent_rule(
                runtime.permission_settings, rule_id, confirm=confirm,
            )
    except GrantError as exc:
        raise SessionServiceError(str(exc)) from exc
    if result.get("scope") == "session":
        service.persist()
    service.publisher.publish("session.policy_updated", {})
    return result


def change_directory_grant(service: Any, **fields: Any) -> dict[str, Any]:
    require_idle(service)
    runtime = service.runtime
    try:
        result = change_directory(
            runtime.session_state,
            runtime.permission_settings,
            fields["path"],
            scope=fields.get("scope", "session"),
            confirm=bool(fields.get("confirm")),
            action=fields["action"],
        )
    except GrantError as exc:
        raise SessionServiceError(str(exc)) from exc
    if result.get("scope") == "session":
        service.persist()
    service.publisher.publish("session.policy_updated", {})
    return result


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
    "change_directory_grant",
    "memory_binding",
    "require_idle",
    "review_action",
    "revoke_grant",
    "session_grants",
    "store_session_document",
]
