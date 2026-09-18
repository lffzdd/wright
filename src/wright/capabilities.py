"""Stable capability assembly for root, child and durable Agent runs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .tools.base import Tool


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


@dataclass(frozen=True)
class CapabilitySnapshot:
    """Per-run immutable view used for schema, search and execution lookup."""

    profile: AgentProfile
    tools: tuple[Tool, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.tools)

    def registry(self) -> dict[str, Tool]:
        return {tool.name: tool for tool in self.tools}


class CapabilityCatalog:
    """The one registration source; rejects aliases or names that collide."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
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
