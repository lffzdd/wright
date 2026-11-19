from __future__ import annotations

import json

from tests.responses import event, response
from wright.application.composition.agent_factory import create_agent
from wright.application.tool_execution.tool_search import make_tool_search_tool
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolAccess, ToolResult
from wright.domain.model.tool_definition import Tool
from wright.domain.model.tool_runtime import ToolRuntime
from wright.domain.protocol import DEFERRED_STUB_LIMIT, call_tool_only, encode_tools
from wright.infrastructure.tools import tools as built_in_tools
from wright.infrastructure.tools.web_search import optional_web_search_tools


def _tool(name: str, description: str, *, deferred: bool = False) -> Tool:
    return Tool(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        call=lambda args, runtime: ToolResult.success(),
        defer_to_model=deferred,
    )


def _search(tools):
    return make_tool_search_tool(tools)


def _found(result) -> list[str]:
    return [item["name"] for item in result.data["tools"]]


def test_tool_search_returns_full_schemas_as_instruction_content():
    core = _tool("read", "Read a file")
    schedule = Tool(
        name="create_schedule",
        description="Create a durable recurring scheduled task",
        parameters={
            "type": "object",
            "properties": {"cron": {"type": "string"}},
            "required": ["cron"],
        },
        call=lambda args, runtime: ToolResult.success(),
        defer_to_model=True,
    )

    result = _search([core, schedule]).call(
        {"query": "schedule recurring task"}, ToolRuntime(tool_name="tool_search")
    )

    assert result.ok
    assert result.data["tools"] == [{
        "name": "create_schedule",
        "description": "Create a durable recurring scheduled task",
        "parameters": schedule.parameters,
        "invoke": "direct",
    }]
    assert "call_tool" not in result.data["note"]
    assert result.retention == "instruction"
    assert result.retention_key == "tool_search:create_schedule"


def test_tool_search_without_matches_is_an_ordinary_result():
    result = _search([_tool("create_schedule", "Schedule", deferred=True)]).call(
        {"query": "unrelated words"}, ToolRuntime(tool_name="tool_search")
    )

    assert result.ok
    assert result.data["tools"] == []
    assert result.retention == ""


def test_tool_search_defaults_to_five_results():
    tools = [
        _tool(f"task_tool_{index}", "Handle a specialized task", deferred=True)
        for index in range(7)
    ]

    result = _search(tools).call(
        {"query": "task"}, ToolRuntime(tool_name="tool_search")
    )

    assert result.ok
    assert len(result.data["tools"]) == 5


def test_search_supports_common_chinese_capability_terms():
    tools = [
        _tool("http_request", "Send an HTTP request", deferred=True),
        _tool("create_schedule", "Schedule a recurring task", deferred=True),
    ]

    network = _search(tools).call(
        {"query": "发送网络请求"}, ToolRuntime(tool_name="tool_search")
    )
    schedule = _search(tools).call(
        {"query": "创建定时任务"}, ToolRuntime(tool_name="tool_search")
    )

    assert _found(network)[0] == "http_request"
    assert _found(schedule)[0] == "create_schedule"


def test_search_drops_stop_word_only_false_positives():
    tools = [
        _tool("write", "Create or overwrite a file", deferred=True),
        _tool("replan", "Create a replacement plan", deferred=True),
        _tool("cancel_schedule", "Cancel a running schedule", deferred=True),
    ]

    result = _search(tools).call(
        {"query": "write a new file"}, ToolRuntime(tool_name="tool_search")
    )

    assert _found(result) == ["write"]


def test_search_reports_baseline_tools_instead_of_activating_lookalikes():
    directory = _tool("ls", "List the immediate children of a directory")
    schedules = _tool(
        "list_schedules", "List durable schedules", deferred=True
    )
    result = _search([directory, schedules]).call(
        {"query": "list directory"}, ToolRuntime(tool_name="tool_search")
    )

    assert result.data["already_available"] == ["ls"]
    assert result.data["tools"] == []


def test_search_does_not_activate_unrelated_list_tools():
    result = _search(
        [_tool("list_schedules", "List durable schedules", deferred=True)],
    ).call(
        {"query": "read write edit files list directory"},
        ToolRuntime(tool_name="tool_search"),
    )

    assert result.data["tools"] == []


def test_encode_tools_sends_deferred_tools_as_stubs():
    deferred = Tool(
        name="special",
        description="Run a specialized job. It has many options.\nMore detail.",
        parameters={
            "type": "object",
            "properties": {"job": {"type": "string"}},
            "required": ["job"],
        },
        call=lambda args, runtime: ToolResult.success(),
        defer_to_model=True,
    )
    core = _tool("read", "Read a file")
    schemas, names = encode_tools([core, deferred])

    assert [item["name"] for item in schemas] == ["read", "special"]
    stub = schemas[1]
    assert stub["parameters"] == {"type": "object"}
    assert stub["description"].startswith("Run a specialized job.")
    assert "many options" not in stub["description"]
    assert "tool_search" in stub["description"]
    assert names["special"] == "special"


def _filler(count: int) -> list[Tool]:
    return [
        _tool(f"filler_{index}", "Filler specialized tool", deferred=True)
        for index in range(count)
    ]


def test_deferred_tools_past_the_stub_limit_go_through_call_tool():
    mcp = _tool("mcp__srv__lookup", "Look something up", deferred=True)
    local = _tool("local_extra", "Local extra tool", deferred=True)
    # MCP tools are listed after built-in ones, so they overflow first.
    tools = [mcp, *_filler(DEFERRED_STUB_LIMIT - 1), local]

    overflow = call_tool_only(tools)
    schemas, _ = encode_tools(tools)

    assert overflow == {"mcp__srv__lookup"}
    sent = {item["name"] for item in schemas}
    assert "local_extra" in sent
    assert "mcp__srv__lookup" not in sent
    assert len(sent) == DEFERRED_STUB_LIMIT

    result = _search(tools).call(
        {"names": ["mcp__srv__lookup", "local_extra"]},
        ToolRuntime(tool_name="tool_search"),
    )
    invoke = {item["name"]: item["invoke"] for item in result.data["tools"]}
    assert invoke == {"mcp__srv__lookup": "call_tool", "local_extra": "direct"}
    assert "call_tool" in result.data["note"]


def test_builtin_capabilities_use_hybrid_loading():
    """Coding, plan, and web tools stay on every request."""
    assert built_in_tools
    by_name = {tool.name: tool for tool in built_in_tools}
    core = {
        "ls",
        "glob",
        "grep",
        "read",
        "write",
        "edit",
        "shell",
        "update_plan",
        "web_fetch",
        "http_request",
    }

    assert core <= by_name.keys()
    assert all(not tool.defer_to_model for tool in built_in_tools)

    schemas, _ = encode_tools(built_in_tools)
    assert {
        item["name"] for item in schemas
    } == core


def test_core_tools_are_baseline_and_follow_ups_are_deferred(tmp_path):
    from wright.application.agent import build_agent_tools
    from wright.application.composition.memory_assembly import (
        assemble_memory_manager,
        memory_tools,
    )

    class UnusedLLM:
        context_limit = 128_000

    search = optional_web_search_tools({"TAVILY_API_KEY": "test"})
    tools = [
        *build_agent_tools(
            UnusedLLM(),
            [*built_in_tools, *search],
            depth=0,
            max_depth=1,
            enable_scheduling=True,
        ),
        *memory_tools(assemble_memory_manager(None, directory=tmp_path / "memory")),
    ]
    by_name = {tool.name: tool for tool in tools}
    baseline = {
        "web_search",
        "http_request",
        "spawn_agent",
        "wait_agent",
        "shell_output",
        "shell_kill",
        "memory_write",
        "memory_search",
        "memory_read",
    }
    deferred = {
        "get_agent_tree",
        "get_agent",
        "cancel_agent",
        "list_commands",
        "memory_update",
        "memory_forget",
        "memory_space",
        "commitment_create",
        "commitment_update",
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
    }
    assert all(not by_name[name].defer_to_model for name in baseline)
    assert all(by_name[name].defer_to_model for name in deferred)




class _ScriptLLM:
    context_limit = 128_000

    def __init__(self, script):
        self.script = list(script)
        self.schema_names: list[list[str]] = []
        self.tool_payloads: list[list[dict]] = []
        self.requests: list[list[dict]] = []

    def __call__(self, messages, *, tools):
        self.schema_names.append([item["name"] for item in tools])
        self.tool_payloads.append(list(tools))
        self.requests.append(list(messages))
        yield event(self.script.pop(0))


def _recording_tool(name: str, invoked: list, **kwargs) -> Tool:
    return Tool(
        name=name,
        description=kwargs.pop("description", f"Specialized {name}"),
        parameters=kwargs.pop("parameters", {"type": "object", "properties": {}}),
        call=lambda args, runtime: invoked.append((name, args)) or ToolResult.success(),
        access_descriptor=kwargs.pop(
            "access_descriptor", lambda args: ToolAccess.internal_read()
        ),
        **kwargs,
    )


def _tool_results(session: Session) -> list[dict]:
    results = []
    for record in session.message_records:
        message = record.message
        if message.get("role") == "tool":
            results.append(json.loads(message["content"]))
    return results


def _overflow_tools(invoked: list, **kwargs) -> list[Tool]:
    """A catalog large enough that the MCP target only runs through call_tool."""
    return [
        *_filler(DEFERRED_STUB_LIMIT),
        _recording_tool("mcp__srv__create", invoked, defer_to_model=True, **kwargs),
    ]


def test_tool_schemas_stay_identical_across_search_call_and_mode_switch(tmp_path):
    invoked: list = []
    specialized = _recording_tool(
        "create_schedule",
        invoked,
        description="Create a durable recurring scheduled task",
        defer_to_model=True,
    )
    writer = _recording_tool(
        "write",
        invoked,
        access_descriptor=lambda args: ToolAccess(frozenset({"file_write"})),
    )
    llm = _ScriptLLM([
        response(calls=[{
            "name": "tool_search",
            "arguments": {"query": "schedule recurring task"},
        }]),
        response(calls=[{"name": "create_schedule", "arguments": {}}]),
        response(content="done"),
        response(calls=[{"name": "write", "arguments": {}}]),
        response(content="read only"),
    ])
    session = Session.create("stable", tmp_path)
    agent = create_agent(llm, [writer, specialized], session)

    assert agent.run("schedule it") == "done"
    session.authorization.set_modes(permission_mode="default", interaction_mode="ask")
    assert agent.run("write it") == "read only"

    assert invoked == [("create_schedule", {})]
    assert llm.schema_names == [["write", "create_schedule", "tool_search"]] * 5
    assert len({json.dumps(tools, sort_keys=True) for tools in llm.tool_payloads}) == 1


def test_restricted_mode_is_announced_at_the_end_of_the_request(tmp_path):
    llm = _ScriptLLM([response(content="a"), response(content="b")])
    session = Session.create("notice", tmp_path)
    agent = create_agent(llm, [_recording_tool("read", [])], session)

    assert agent.run("first") == "a"
    session.authorization.set_modes(permission_mode="default", interaction_mode="plan")
    assert agent.run("second") == "b"

    assert "<interaction-mode>" not in json.dumps(llm.requests[0])
    last = llm.requests[1][-1]
    assert "<interaction-mode>" in last["content"]
    assert "plan" in last["content"]


def test_stub_tool_is_validated_against_its_real_schema(tmp_path):
    invoked: list = []
    parameters = {
        "type": "object",
        "properties": {"cron": {"type": "string"}},
        "required": ["cron"],
        "additionalProperties": False,
    }
    specialized = _recording_tool(
        "create_schedule", invoked, parameters=parameters, defer_to_model=True
    )
    llm = _ScriptLLM([
        response(calls=[{"name": "create_schedule", "arguments": {"cron": 5}}]),
        response(calls=[{"name": "create_schedule", "arguments": {"cron": "* * * * *"}}]),
        response(content="done"),
    ])
    session = Session.create("stub", tmp_path)

    assert create_agent(llm, [specialized], session).run("schedule it") == "done"

    first, second = _tool_results(session)
    assert first["ok"] is False
    assert first["data"]["parameters"] == parameters
    assert second["ok"] is True
    assert invoked == [("create_schedule", {"cron": "* * * * *"})]
    # The schema it returned must survive folding like a tool_search result.
    assert first["retention"] == "instruction"


def test_call_tool_validates_arguments_of_the_real_tool(tmp_path):
    invoked: list = []
    parameters = {
        "type": "object",
        "properties": {"cron": {"type": "string"}},
        "required": ["cron"],
        "additionalProperties": False,
    }
    llm = _ScriptLLM([
        response(calls=[{
            "name": "call_tool",
            "arguments": {"name": "mcp__srv__create", "arguments": {"cron": 5}},
        }]),
        response(calls=[{
            "name": "call_tool",
            "arguments": {"name": "mcp__srv__create", "arguments": {"cron": "* * * * *"}},
        }]),
        response(content="done"),
    ])
    session = Session.create("validate", tmp_path)
    tools = _overflow_tools(invoked, parameters=parameters)

    assert create_agent(llm, tools, session).run("schedule it") == "done"

    assert "call_tool" in llm.schema_names[0]
    assert "mcp__srv__create" not in llm.schema_names[0]
    first, second = _tool_results(session)
    assert first["ok"] is False
    assert first["data"]["parameters"] == parameters
    assert second["ok"] is True
    assert invoked == [("mcp__srv__create", {"cron": "* * * * *"})]


def test_call_tool_is_only_offered_when_tools_overflow(tmp_path):
    llm = _ScriptLLM([response(content="done")])
    session = Session.create("no-overflow", tmp_path)
    tools = [_recording_tool("create_schedule", [], defer_to_model=True)]

    assert create_agent(llm, tools, session).run("go") == "done"

    assert llm.schema_names == [["create_schedule", "tool_search"]]


def test_call_tool_rejects_unknown_baseline_and_hidden_targets(tmp_path):
    invoked: list = []
    tools = [
        _recording_tool("read", invoked),
        _recording_tool("internal", invoked, defer_to_model=True, expose_to_model=False),
        *_overflow_tools(invoked),
    ]
    llm = _ScriptLLM([
        response(calls=[
            {"name": "call_tool", "arguments": {"name": "nope", "arguments": {}}},
            {"name": "call_tool", "arguments": {"name": "read", "arguments": {}}},
            {"name": "call_tool", "arguments": {"name": "internal", "arguments": {}}},
            {"name": "call_tool", "arguments": {"name": "call_tool", "arguments": {}}},
        ]),
        response(content="done"),
    ])
    session = Session.create("reject", tmp_path)

    assert create_agent(llm, tools, session).run("go") == "done"

    results = _tool_results(session)
    assert [item["ok"] for item in results] == [False, False, False, False]
    assert "tool_search" in results[0]["err"]
    assert "directly" in results[1]["err"]
    assert invoked == []


def test_interaction_mode_blocks_the_real_tool_at_execution(tmp_path):
    invoked: list = []
    access = lambda args: ToolAccess(frozenset({"persistent_write"}))  # noqa: E731
    stubbed = _recording_tool(
        "create_schedule", invoked, defer_to_model=True, access_descriptor=access
    )
    llm = _ScriptLLM([
        response(calls=[
            {"name": "create_schedule", "arguments": {}},
            {"name": "call_tool", "arguments": {"name": "mcp__srv__create", "arguments": {}}},
        ]),
        response(content="done"),
    ])
    session = Session.create("ask-mode", tmp_path)
    session.authorization.set_modes(permission_mode="default", interaction_mode="ask")
    tools = [stubbed, *_overflow_tools(invoked, access_descriptor=access)]

    assert create_agent(llm, tools, session).run("go") == "done"

    # The schema list still offers the tool; execution refuses it.
    assert "create_schedule" in llm.schema_names[0]
    direct, wrapped = _tool_results(session)
    for result, name in ((direct, "create_schedule"), (wrapped, "mcp__srv__create")):
        assert result["ok"] is False
        assert name in result["err"]
        assert "ask mode" in result["err"]
    assert invoked == []


def test_overflow_tool_called_directly_fails_only_that_call(tmp_path):
    invoked: list = []
    tools = [_recording_tool("read", invoked), *_overflow_tools(invoked)]
    llm = _ScriptLLM([
        response(calls=[
            {"name": "mcp__srv__create", "arguments": {}},
            {"name": "read", "arguments": {}},
        ]),
        response(content="done"),
    ])
    session = Session.create("direct", tmp_path)

    assert create_agent(llm, tools, session).run("go") == "done"

    deferred, read = _tool_results(session)
    assert deferred["ok"] is False
    assert "call_tool" in deferred["err"]
    assert read["ok"] is True
    assert invoked == [("read", {})]
    assert all(turn.route != "invalid" for turn in session.turns)


def test_searched_schemas_survive_request_folding(tmp_path):
    specialized = _recording_tool(
        "create_schedule",
        [],
        description="Create a durable recurring scheduled task",
        defer_to_model=True,
    )
    big = _recording_tool("read", [])
    big.call = lambda args, runtime: ToolResult.success("x" * 40_000)
    llm = _ScriptLLM([
        response(calls=[{
            "name": "tool_search",
            "arguments": {"query": "schedule recurring task"},
        }]),
        response(calls=[{"name": "read", "arguments": {}}]),
        response(content="done"),
    ])
    llm.context_limit = 16_000
    session = Session.create("fold", tmp_path)
    agent = create_agent(
        llm,
        [big, specialized],
        session,
        context_watermark=0.2,
        keep_recent_tool_results=0,
    )

    assert agent.run("go") == "done"

    last = json.dumps(llm.requests[-1], ensure_ascii=False)
    assert "Create a durable recurring scheduled task" in last
    assert "x" * 1_000 not in last
