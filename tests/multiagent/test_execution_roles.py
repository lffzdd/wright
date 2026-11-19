"""Role selection is declared on each tool and defaults to root only."""

from __future__ import annotations

from wright.application.execution.roles import tools_for_role
from wright.domain.model.tool import ToolResult
from wright.domain.model.tool_definition import ALL_ROLES, INTERACTIVE_ROLES, Tool
from wright.infrastructure.tools import ask_user_tool
from wright.infrastructure.tools import tools as base_tools
from wright.infrastructure.tools.loop_tools import manage_loop_tool


def _tool(name: str, **kwargs) -> Tool:
    return Tool(
        name,
        "",
        {"type": "object"},
        lambda _args, _rt: ToolResult.success(),
        **kwargs,
    )


def _names(tools, role) -> set[str]:
    return {tool.name for tool in tools_for_role(tools, role)}


def test_undeclared_tool_stays_with_root():
    tools = [_tool("new_root_tool")]

    assert _names(tools, "root") == {"new_root_tool"}
    assert _names(tools, "child") == set()
    assert _names(tools, "durable") == set()


def test_declared_roles_are_honored():
    tools = [
        _tool("everywhere", roles=ALL_ROLES),
        _tool("interactive", roles=INTERACTIVE_ROLES),
    ]

    assert _names(tools, "child") == {"everywhere", "interactive"}
    assert _names(tools, "durable") == {"everywhere"}


def test_shared_base_tools_reach_child_and_durable():
    names = {tool.name for tool in base_tools}

    assert _names(base_tools, "child") == names
    assert _names(base_tools, "durable") == names


def test_root_only_builtins_do_not_reach_child_or_durable():
    tools = [ask_user_tool, manage_loop_tool]

    assert _names(tools, "child") == set()
    assert _names(tools, "durable") == set()
