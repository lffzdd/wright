"""Tool value objects and parameter contracts (Domain Model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ...base.value_object import ValueObject
from ..tool_protocol import (
    AccessTarget,
    ArtifactRef,
    PermissionOperation,
    ToolAccess,
    ToolCall,
    ToolResult,
)


@dataclass(frozen=True)
class ToolDefinition(ValueObject):
    """Immutable tool value object defining metadata and parameter schema."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    requires_user_interaction: bool = False

    def to_schema(self) -> dict[str, Any]:
        """Convert to OpenAI / function-calling schema representation."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


__all__ = [
    "AccessTarget",
    "ArtifactRef",
    "PermissionOperation",
    "ToolAccess",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
]
