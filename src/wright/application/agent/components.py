"""Values shared by the agent run loop and the assembler.

Neither side imports the other through this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.model.agent import AgentProfile, CapabilitySnapshot
from ...infrastructure.tools.base import Tool
from ..tool_execution.dispatch import ToolDispatchService
from .cancellation import CancellationToken
from .context import ContextBuilder


@dataclass
class PreparedTools:
    """The model-facing tool list for one agent, built once before it runs."""

    profile: AgentProfile
    capabilities: CapabilitySnapshot
    tools: list[Tool]
    schemas: list[dict]
    names: dict[str, str]


@dataclass
class AgentComponents:
    """Per-Agent collaborators assembled outside the Agent behavior object."""

    cancellation: CancellationToken
    context_builder: ContextBuilder
    tool_dispatcher: ToolDispatchService

    @property
    def executor(self) -> ToolDispatchService:
        return self.tool_dispatcher
