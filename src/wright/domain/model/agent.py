"""Agent profile, state machine, and capability domain models."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from .tool import ModelVisibleTool


class AgentState(str, Enum):
    """Lifecycle state machine for an agent."""
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"


class CapabilityError(ValueError):
    pass


@dataclass(frozen=True)
class AgentProfile:
    """Requested capabilities; hosts still impose the final authorization."""

    name: str
    allowed_tools: frozenset[str]
    max_steps: int | None = None
    allow_interaction: bool = False
    allow_background_tasks: bool = False
    allow_delegation: bool = False
    role_instruction: str = ""


@dataclass(frozen=True)
class CapabilitySnapshot:
    """Per-run immutable view used for schema, search and execution lookup."""

    profile: AgentProfile
    tools: tuple[ModelVisibleTool, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.tools)

    def registry(self) -> dict[str, ModelVisibleTool]:
        return {tool.name: tool for tool in self.tools}


class CapabilityCatalog:
    """The one registration source; rejects aliases or names that collide."""

    def __init__(self, tools: Iterable[ModelVisibleTool]) -> None:
        self._tools: dict[str, ModelVisibleTool] = {}
        for tool in tools:
            if not tool.name or tool.name in self._tools:
                raise CapabilityError(f"duplicate capability name: {tool.name!r}")
            self._tools[tool.name] = tool

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def snapshot(self, profile: AgentProfile) -> CapabilitySnapshot:
        unknown = profile.allowed_tools - self.names
        if unknown:
            raise CapabilityError(f"profile references unknown capabilities: {sorted(unknown)}")
        return CapabilitySnapshot(
            profile=profile,
            tools=tuple(tool for name, tool in self._tools.items() if name in profile.allowed_tools),
        )


__all__ = [
    "AgentProfile",
    "AgentState",
    "CapabilityCatalog",
    "CapabilityError",
    "CapabilitySnapshot",
]
