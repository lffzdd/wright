from __future__ import annotations

from ..agent import Agent
from ..prompt import build_system_prompt
from ..renderer import SilentRenderer
from ..session import Session
from ..tools.base import Tool, ToolResult, split_tool_catalog
from ..tools.tool_search import make_tool_search_tool


def _tool(name: str, *, deferred: bool = False, expose: bool = True) -> Tool:
    return Tool(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        call=lambda args, runtime: ToolResult.success(),
        defer_to_model=deferred,
        expose_to_model=expose,
    )


def test_catalog_lists_follow_assembled_tools():
    tools = [
        _tool("read_file"),
        _tool("brand_new"),
        _tool("schedule_task", deferred=True),
        _tool("hidden_widget", deferred=True),
        _tool("internal_only", expose=False),
    ]
    baseline, deferred = split_tool_catalog(tools)
    assert baseline == ["read_file", "brand_new"]
    assert deferred == ["schedule_task", "hidden_widget"]

    prompt = build_system_prompt(tools)
    assert "Always available: read_file, brand_new." in prompt
    assert "hidden_widget" not in prompt
    assert "schedule_task" not in prompt
    assert "tool_search" in prompt
    assert "Prefer edit_file" not in prompt
    assert "execute_command" not in prompt


def test_system_prompt_omits_search_when_nothing_is_deferred():
    prompt = build_system_prompt([_tool("read_file"), _tool("write_file")])
    assert "tool_search" not in prompt


def test_edit_and_shell_advice_only_when_those_tools_exist():
    with_edit = build_system_prompt([
        _tool("read_file"), _tool("edit_file"), _tool("write_file")
    ])
    assert "Prefer edit_file" in with_edit
    assert "replace_all" in with_edit
    assert "read_file before edit_file" in with_edit
    assert "overwriting it with write_file" in with_edit
    assert "N|" in with_edit
    assert "execute_command" not in with_edit

    with_shell = build_system_prompt([_tool("execute_command")])
    assert "Prefer edit_file" not in with_shell
    assert "execute_command keeps a working directory" in with_shell


def test_tool_search_description_picks_up_a_new_deferred_tool():
    search = make_tool_search_tool(
        [_tool("read_file"), _tool("new_hidden", deferred=True)],
        [],
    )
    assert "new_hidden" in search.description
    assert "read_file" not in search.description


def test_agent_system_prompt_uses_the_assembled_catalog(tmp_path):
    class UnusedLLM:
        context_limit = 128_000

    session = Session.create("t", tmp_path)
    agent = Agent(
        UnusedLLM(),
        [_tool("read_file"), _tool("brand_new"), _tool("new_hidden", deferred=True)],
        session,
        SilentRenderer(),
    )
    content = session.message_records[0].message["content"]
    assert "Always available: read_file, brand_new." in content
    assert "new_hidden" not in content
    search = next(tool for tool in agent._schema_tools if tool.name == "tool_search")
    assert "new_hidden" in search.description
