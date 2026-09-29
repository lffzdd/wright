"""Cross-type identifier checks.

Specialized tools read native records. This object only answers which family
owns an id, so a control action can fail before it touches the wrong lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from ...domain.model.agent import AgentControlError
from ..scheduling.contracts import JobNotFoundError

ExecutionKind = Literal["agent", "command", "run", "schedule"]

_LABELS: dict[str, str] = {
    "agent": "delegated agent execution (agent_task_id)",
    "command": "command execution (command_id)",
    "run": "schedule run (run_id)",
    "schedule": "schedule (schedule_id)",
}


class ExecutionNotFound(LookupError):
    def __init__(self, kind: ExecutionKind, identifier: str) -> None:
        self.kind = kind
        self.identifier = identifier
        super().__init__(f"Unknown {_LABELS[kind]}: {identifier}")


class ExecutionKindMismatch(ValueError):
    def __init__(
        self, identifier: str, *, expected: ExecutionKind, actual: ExecutionKind
    ) -> None:
        self.identifier = identifier
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{identifier} belongs to a {_LABELS[actual]} and cannot be used as a "
            f"{_LABELS[expected]}"
        )


class ExecutionWaitCancelled(RuntimeError):
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        super().__init__(f"wait cancelled: {identifier}")


class ExecutionIdentity:
    """Read-only probes. None of these callables may cancel or mutate."""

    def __init__(
        self,
        *,
        has_agent: Callable[[str], bool],
        has_command: Callable[[str], bool],
        has_run: Callable[[str], bool],
        has_schedule: Callable[[str], bool],
    ) -> None:
        self._probes: dict[ExecutionKind, Callable[[str], bool]] = {
            "agent": has_agent,
            "command": has_command,
            "run": has_run,
            "schedule": has_schedule,
        }

    def classify(self, identifier: str) -> ExecutionKind | None:
        found = [kind for kind, probe in self._probes.items() if probe(identifier)]
        if len(found) > 1:
            raise ExecutionKindMismatch(
                identifier, expected=found[0], actual=found[1]
            )
        return found[0] if found else None

    def require(self, identifier: str, expected: ExecutionKind) -> None:
        actual = self.classify(identifier)
        if actual is None:
            raise ExecutionNotFound(expected, identifier)
        if actual != expected:
            raise ExecutionKindMismatch(
                identifier, expected=expected, actual=actual
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
