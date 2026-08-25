"""Shared, read-only task model exposed to callers and the LLM.

Execution-specific state remains in its owner (the Agent control plane or the
shell background-task registry).  ``RuntimeTask`` is a projection, not another
state machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TaskKind = Literal["agent", "shell", "durable"]
TaskStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled", "timed_out",
    "unknown",
]
TERMINAL_TASK_STATUSES = frozenset({
    "completed", "failed", "cancelled", "timed_out", "unknown"
})


@dataclass(frozen=True)
class RuntimeTask:
    """Common task view returned by every backend.

    ``details`` preserves backend-specific information for diagnostics and
    compatibility adapters.  Generic task tools should depend on the common
    fields instead of branching on those details.
    """

    id: str
    kind: TaskKind
    status: TaskStatus
    description: str
    root_turn_id: str = ""
    parent_id: str | None = None
    created_at: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    result: str = ""
    output: str = ""
    error: str = ""
    returncode: int | None = None
    cancel_requested: bool = False
    cancel_reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "terminal": self.terminal,
            "description": self.description,
            "root_turn_id": self.root_turn_id,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "result": self.result,
            "output": self.output,
            "error": self.error,
            "returncode": self.returncode,
            "cancel_requested": self.cancel_requested,
            "cancel_reason": self.cancel_reason,
        }
        if include_details:
            value["details"] = dict(self.details)
        return value
