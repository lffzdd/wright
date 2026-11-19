"""Which tools each execution role may receive.

Each tool declares its roles on ``Tool.roles``; the default is root only.
Root, child, and durable selection lives here so sub-agent creation and the
durable runner share one rule. A tool missing a declaration stays with the
root instead of leaking into an isolated child or an unattended run.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace

from ...domain.model.tool_definition import ExecutionRole, Tool


def tools_for_role(tools: Sequence[Tool], role: ExecutionRole) -> list[Tool]:
    selected: list[Tool] = []
    for tool in tools:
        if role not in tool.roles:
            continue
        if tool.name == "shell" and role != "root":
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
    "ExecutionRole",
    "tools_for_role",
]
