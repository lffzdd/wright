"""Persistent execution records, separate from the live session shell."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

RunStatus = Literal["queued", "running", "waiting_interaction", "cancelling", "completed", "failed", "cancelled", "interrupted"]
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    source: str
    goal: str
    parent_run_id: str | None = None
    root_run_id: str = ""
    status: RunStatus = "queued"
    started_at: float | None = None
    ended_at: float | None = None
    result: str = ""
    error: str = ""
    model_config: dict[str, str] = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    step_ids: list[str] = field(default_factory=list)
    tool_execution_ids: list[str] = field(default_factory=list)

    def start(self) -> None:
        if self.status == "queued":
            self.status, self.started_at = "running", time.time()

    def finish(self, status: RunStatus, *, result: str = "", error: str = "") -> None:
        if self.status in TERMINAL_RUN_STATUSES:
            return
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("run finish requires a terminal status")
        self.status, self.result, self.error, self.ended_at = status, result, error, time.time()

    def resume_after_rejection(self) -> None:
        """A completion candidate rejected by a hook is not terminal."""
        self.status, self.result, self.error, self.ended_at = "running", "", "", None


def new_run_id() -> str:
    return f"run_{uuid4().hex}"
