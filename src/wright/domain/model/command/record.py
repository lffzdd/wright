"""Persistable identity of one shell execution.

Process handles, output buffers, and completion callbacks stay in the command
runtime. This record is the part a session may checkpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

CommandDisposition = Literal[
    "running", "completed", "failed", "cancelled", "unknown"
]


@dataclass
class CommandRecord:
    command_id: str
    command: str
    root_turn_id: str = ""
    run_id: str = ""
    created_at: float = 0.0
    started_at: float = 0.0
    ended_at: float | None = None
    returncode: int | None = None
    cancel_requested: bool = False
    cancel_reason: str = ""
    disposition: CommandDisposition = "running"

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()
        if not self.started_at:
            self.started_at = self.created_at


__all__ = ["CommandDisposition", "CommandRecord"]
