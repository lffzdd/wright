"""Which tools each execution role may receive.

Root, child, and durable selection lives here so sub-agent creation and the
durable runner do not keep separate name blacklists.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Literal

from ...infrastructure.tools.base import Tool

ExecutionRole = Literal["root", "child", "durable"]

# Names that must never be handed to an isolated child, even if a caller
# passes them in the base catalog.
CHILD_EXCLUDED_TOOLS = frozenset({
    "get_agent",
    "wait_agent",
    "cancel_agent",
    "get_agent_tree",
    "get_command",
    "wait_command",
    "terminate_command",
    "list_commands",
    "create_schedule",
    "get_schedule",
    "list_schedules",
    "pause_schedule",
    "resume_schedule",
    "cancel_schedule",
    "list_schedule_runs",
    "get_schedule_run",
    "wait_schedule_run",
    "cancel_schedule_run",
    "ask_user",
    "manage_loop",
    "load_skill",
})

# Extra removals for an unattended durable worker. Memory and external search
# stay off that worker even when a child of the interactive root could see them.
DURABLE_EXCLUDED_TOOLS = CHILD_EXCLUDED_TOOLS | frozenset({
    "create_memory",
    "get_memory",
    "update_memory",
    "delete_memory",
    "search_memory",
    "search_episodes",
    "get_episode",
    "delete_episode",
    "knowledge_search",
})

ROOT_ONLY_TOOLS = CHILD_EXCLUDED_TOOLS


def tools_for_role(tools: Sequence[Tool], role: ExecutionRole) -> list[Tool]:
    denied = DURABLE_EXCLUDED_TOOLS if role == "durable" else (
        CHILD_EXCLUDED_TOOLS if role == "child" else frozenset()
    )
    selected: list[Tool] = []
    for tool in tools:
        if tool.name in denied:
            continue
        if tool.name == "execute_command" and role != "root":
            selected.append(_foreground_command(tool))
            continue
        selected.append(tool)
    return selected


def _foreground_command(tool: Tool) -> Tool:
    parameters = deepcopy(tool.parameters)
    properties = parameters.get("properties", {})
    if isinstance(properties, dict):
        properties["run_in_background"] = {
            "type": "boolean",
            "const": False,
            "default": False,
            "description": "Must be false; this Agent cannot leave background processes",
        }
    return replace(
        tool,
        description=(
            "Execute a foreground shell command in the shared workspace. "
            "This Agent cannot create or retain background processes."
        ),
        parameters=parameters,
    )


__all__ = [
    "CHILD_EXCLUDED_TOOLS",
    "DURABLE_EXCLUDED_TOOLS",
    "ROOT_ONLY_TOOLS",
    "tools_for_role",
]
