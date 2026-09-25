"""Tools for inspecting and autonomously updating the Agent's Core Memory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...persistence.memory import FileCoreMemoryStore
from ....domain.policy.memory import CoreMemoryPolicy
from ....domain.model.tool import AccessTarget, ToolAccess, ToolResult
from ..base import Tool
from ..runtime import ToolRuntime


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
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    """Read the current pinned core memory (persona, human profile, project anchor)."""
    store = FileCoreMemoryStore(directory)
    mem = store.load()
    return ToolResult.success(mem.to_dict())


def update_core_memory(
    section: str,
    content: str,
    mode: str = "append",
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    """Autonomously update human profile or project anchor in the agent's core memory."""
    policy = CoreMemoryPolicy()
    is_valid, error_msg = policy.validate_update(section, content)
    if not is_valid:
        return ToolResult.fail(error_msg or "Validation failed")

    store = FileCoreMemoryStore(directory)
    mem = store.load()

    normalized_section = section.strip().lower()
    new_text = content.strip()

    if normalized_section == "human_profile":
        if mode == "append" and mem.human_profile:
            final_text = f"{mem.human_profile}\n- {new_text}"
        else:
            final_text = new_text
        is_len_valid, len_err = policy.validate_update(normalized_section, final_text)
        if not is_len_valid:
            return ToolResult.fail(len_err or "Exceeds length limit")
        mem.update_human_profile(final_text)
    elif normalized_section == "project_anchor":
        if mode == "append" and mem.project_anchor:
            final_text = f"{mem.project_anchor}\n- {new_text}"
        else:
            final_text = new_text
        is_len_valid, len_err = policy.validate_update(normalized_section, final_text)
        if not is_len_valid:
            return ToolResult.fail(len_err or "Exceeds length limit")
        mem.update_project_anchor(final_text)
    else:
        return ToolResult.fail(f"Unsupported section: {section}")

    store.save(mem)
    return ToolResult.success({
        "status": "updated",
        "section": normalized_section,
        "content": final_text,
    })


def build_core_memory_tools(directory: Path | None = None) -> list[Tool]:
    """Construct core memory tools bound to the specified storage directory."""
    def bind(fn):
        return lambda args, runtime=None: fn(**args, runtime=runtime, directory=directory)

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
            call=bind(get_core_memory),
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
            call=bind(update_core_memory),
            access_descriptor=_describe_core_memory_write,
            defer_to_model=True,
        ),
    ]


__all__ = [
    "build_core_memory_tools",
    "get_core_memory",
    "update_core_memory",
]
