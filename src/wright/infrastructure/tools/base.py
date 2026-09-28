"""Model-facing description of a callable tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ...domain.model.tool import ToolAccess

if TYPE_CHECKING:
    from ...domain.model.tool import ToolResult
    from .runtime import ToolRuntime


TimeoutOwner = Literal["executor", "tool"]


def _not_concurrency_safe(args: dict[str, Any]) -> bool:
    """新工具默认排他执行；必须显式声明才允许进入并发批。"""
    del args
    return False


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    call: Callable[[dict[str, Any], ToolRuntime], ToolResult]
    access_descriptor: Callable[[dict[str, Any]], ToolAccess] = (
        lambda args: ToolAccess.unknown()
    )
    is_concurrency_safe: Callable[[dict[str, Any]], bool] = _not_concurrency_safe
    requires_user_interaction: bool = False
    timeout_owner: TimeoutOwner = "executor"
    execution_timeout: float | None = None
    expose_to_model: bool = True
    # Delays schema exposure until tool_search activates the tool.
    # Registration, permission checks, and any startup (for example an MCP
    # connection) are separate and are not deferred by this flag.
    defer_to_model: bool = False
    # Skills disclosure is request-scoped, so the loader is omitted from the
    # frozen system-prompt tool list.
    list_in_system_prompt: bool = True
    source: str = "builtin"
    required_capabilities: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def describe_access(self, arguments: dict[str, Any]) -> ToolAccess:
        """Describe this invocation without performing a side effect."""

        return self.access_descriptor(dict(arguments))


