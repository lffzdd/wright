"""Data Transfer Object for streaming events to presentation layers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

StreamEventType = Literal[
    "text_delta",
    "reasoning_delta",
    "tool_call",
    "tool_result",
    "step_complete",
    "run_complete",
    "error",
]


@dataclass(frozen=True)
class StreamEventDTO:
    """Outbound streaming event payload dispatched to CLI, API, or Web interfaces."""

    event_type: StreamEventType | str
    turn_id: str = ""
    session_id: str = ""
    content: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "content": self.content,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
        }
