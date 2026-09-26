"""Tools for inspecting and autonomously updating the Agent's Core Memory.

These entry adapters delegate core memory reads and updates to MemoryService
and translate application results into ToolResult values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ....domain.model.tool import AccessTarget, ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolRuntime

if TYPE_CHECKING:
    from ....application.memory.memory_service import MemoryService


def _describe_core_memory_read(args: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"file_read"}),
        targets=(AccessTarget(parameter="file", value="core_memory.json", operation="file_read", kind="file"),),
        subject="read core_memory.json",
        reason="inspecting pinned core memory",
    )


def _describe_core_memory_write(args: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"file_write"}),
        targets=(AccessTarget(parameter="file", value="core_memory.json", operation="file_write", kind="file"),),
        subject="update core_memory.json",
        reason="updating agent core memory notes",
    )


def get_core_memory(
    service: MemoryService,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    """Read the current pinned core memory (persona, human profile, project anchor)."""
    mem = service.get_core_memory()
    if mem is None:
        return ToolResult.fail("Core memory store is not configured")
    return ToolResult.success(mem.to_dict())


def update_core_memory(
    service: MemoryService,
    section: str,
    content: str,
    mode: str = "append",
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    """Update core memory through the application service and report its result."""
    updated, err = service.update_core_memory(section, content, mode)
    if updated is None:
        return ToolResult.fail(err or "Update failed")

    return ToolResult.success({
        "status": "updated",
        "section": updated.section,
        "content": updated.content,
    })


def build_core_memory_tools(service: MemoryService) -> list[Tool]:
    """Construct core memory tools bound to the application service."""
    def _bind_get(args: dict[str, Any], runtime: ToolRuntime | None = None) -> ToolResult:
        return get_core_memory(service, runtime=runtime)

    def _bind_update(args: dict[str, Any], runtime: ToolRuntime | None = None) -> ToolResult:
        return update_core_memory(
            service,
            section=args["section"],
            content=args["content"],
            mode=args.get("mode", "append"),
            runtime=runtime,
        )

    return [
        Tool(
            name="get_core_memory",
            description="View the agent's pinned core memory (persona, human profile, and project anchor).",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            call=_bind_get,
            access_descriptor=_describe_core_memory_read,
            is_concurrency_safe=lambda args: True,
            defer_to_model=True,
        ),
        Tool(
            name="update_core_memory",
            description=(
                "Update the agent's pinned core memory. Use this to permanently record enduring user habits, "
                "operating system constraints, or firm project architecture rules. "
                "Allowed sections: 'human_profile', 'project_anchor'. Modifying 'persona' is strictly forbidden."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["human_profile", "project_anchor"],
                        "description": "Section to update: 'human_profile' (user preferences/constraints) or 'project_anchor' (project architecture rules).",
                    },
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The exact note, habit, or rule to record.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["append", "replace"],
                        "default": "append",
                        "description": "Whether to append as a new bullet point or completely replace the section.",
                    },
                },
                "required": ["section", "content"],
                "additionalProperties": False,
            },
            call=_bind_update,
            access_descriptor=_describe_core_memory_write,
            defer_to_model=True,
        ),
    ]


__all__ = [
    "build_core_memory_tools",
    "get_core_memory",
    "update_core_memory",
]
