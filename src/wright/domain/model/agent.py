"""Agent profile, state machine, and capability domain models."""

from __future__ import annotations

from enum import Enum
from ..capabilities import (
    AgentProfile,
    CapabilityCatalog,
    CapabilitySnapshot,
)


class AgentState(str, Enum):
    """Lifecycle state machine for an agent."""
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"


__all__ = [
    "AgentProfile",
    "AgentState",
    "CapabilityCatalog",
    "CapabilitySnapshot",
]
