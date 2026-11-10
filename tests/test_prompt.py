from __future__ import annotations

from wright.application.composition.agent_factory import create_agent
from wright.application.tool_execution.tool_search import make_tool_search_tool
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolResult, split_tool_catalog
from wright.domain.model.tool_definition import Tool
from wright.domain.prompt import build_system_prompt


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
        _tool("read"),
        _tool("brand_new"),
        _tool("create_schedule", deferred=True),
        _tool("hidden_widget", deferred=True),
        _tool("internal_only", expose=False),
    ]
    baseline, deferred = split_tool_catalog(tools)
    assert baseline == ["read", "brand_new"]
    assert deferred == ["create_schedule", "hidden_widget"]

    prompt = build_system_prompt(tools)
    assert "Always available: read, brand_new." in prompt
    assert "hidden_widget" not in prompt
    assert "create_schedule" not in prompt
    assert "tool_search" in prompt
    assert "Prefer edit for" not in prompt
    assert "shell" not in prompt


def test_system_prompt_omits_search_when_nothing_is_deferred():
    prompt = build_system_prompt([_tool("read"), _tool("write")])
    assert "tool_search" not in prompt


def test_edit_and_shell_advice_only_when_those_tools_exist():
    with_edit = build_system_prompt([
        _tool("read"), _tool("edit"), _tool("write")
    ])
    assert "Prefer edit for" in with_edit
    assert "replace_all" in with_edit
    assert "read before edit" in with_edit
    assert "overwriting it with write" in with_edit
    assert "N|" in with_edit
    assert "shell" not in with_edit

    with_shell = build_system_prompt([_tool("shell")])
    assert "Prefer edit for" not in with_shell
    assert "shell keeps a working directory" in with_shell


def test_tool_search_description_picks_up_a_new_deferred_tool():
    search = make_tool_search_tool(
        [_tool("read"), _tool("new_hidden", deferred=True)],
        [],
    )
    assert "new_hidden" in search.description
    assert "Use it for: new_hidden." in search.description


def test_agent_system_prompt_uses_the_assembled_catalog(tmp_path):
    class UnusedLLM:
        context_limit = 128_000

    session = Session.create("t", tmp_path)
    agent = create_agent(
        UnusedLLM(),
        [_tool("read"), _tool("brand_new"), _tool("new_hidden", deferred=True)],
        session,
    )
    content = session.message_records[0].message["content"]
    assert "Always available: read, brand_new." in content
    assert "new_hidden" not in content
    search = next(tool for tool in agent._schema_tools if tool.name == "tool_search")
    assert "new_hidden" in search.description
