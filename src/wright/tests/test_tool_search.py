from __future__ import annotations

from wright.tests.responses import event, response

from ..agent import Agent
from ..protocol import encode_tools
from ..renderer import SilentRenderer
from ..session import Session
from ..tools import tools as built_in_tools
from ..tools.base import Tool, ToolResult, ToolRuntime
from ..tools.tool_search import make_tool_search_tool


def _tool(name: str, description: str, *, deferred: bool = False) -> Tool:
    return Tool(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        call=lambda args, runtime: ToolResult.success(),
        defer_to_model=deferred,
    )


def test_tool_search_activates_specialized_schemas_only_after_search():
    core = _tool("read_file", "Read a file")
    schedule = _tool(
        "schedule_task", "Create a durable recurring scheduled task", deferred=True
    )
    active: list[str] = []
    search = make_tool_search_tool([core, schedule], active)

    before, _ = encode_tools(
        [core, schedule, search], active_deferred=active
    )
    result = search.call(
        {"query": "schedule recurring task"}, ToolRuntime(tool_name="tool_search")
    )
    after, _ = encode_tools(
        [core, schedule, search], active_deferred=active
    )

    assert [item["name"] for item in before] == [
        "read_file", "tool_search"
    ]
    assert result.ok
    assert result.data["activated"][0]["name"] == "schedule_task"
    assert [item["name"] for item in after] == [
        "read_file", "schedule_task", "tool_search"
    ]


def test_tool_search_defaults_to_five_results():
    tools = [
        _tool(f"task_tool_{index}", "Handle a specialized task", deferred=True)
        for index in range(7)
    ]
    active: list[str] = []

    result = make_tool_search_tool(tools, active).call(
        {"query": "task"}, ToolRuntime(tool_name="tool_search")
    )

    assert result.ok
    assert len(result.data["activated"]) == 5
    assert len(active) == 5


def test_repeated_search_fills_with_not_yet_active_tools():
    tools = [
        _tool(f"task_tool_{index}", "Handle a specialized task", deferred=True)
        for index in range(7)
    ]
    active: list[str] = []
    search = make_tool_search_tool(tools, active)

    first = search.call(
        {"query": "task"}, ToolRuntime(tool_name="tool_search")
    )
    second = search.call(
        {"query": "task"}, ToolRuntime(tool_name="tool_search")
    )

    assert len(first.data["activated"]) == 5
    assert [item["name"] for item in second.data["activated"]] == [
        "task_tool_5",
        "task_tool_6",
    ]
    assert len(second.data["already_active"]) == 5
    assert len(active) == 7


def test_search_supports_common_chinese_capability_terms():
    tools = [
        _tool("http_request", "Send an HTTP request", deferred=True),
        _tool("schedule_task", "Schedule a recurring task", deferred=True),
    ]

    network = make_tool_search_tool(tools, []).call(
        {"query": "发送网络请求"}, ToolRuntime(tool_name="tool_search")
    )
    schedule = make_tool_search_tool(tools, []).call(
        {"query": "创建定时任务"}, ToolRuntime(tool_name="tool_search")
    )

    assert network.data["activated"][0]["name"] == "http_request"
    assert schedule.data["activated"][0]["name"] == "schedule_task"


def test_search_drops_stop_word_only_false_positives():
    tools = [
        _tool("write_file", "Create or overwrite a file", deferred=True),
        _tool("replan", "Create a replacement plan", deferred=True),
        _tool("cancel_task", "Cancel a running task", deferred=True),
    ]

    result = make_tool_search_tool(tools, []).call(
        {"query": "write a new file"}, ToolRuntime(tool_name="tool_search")
    )

    assert [item["name"] for item in result.data["activated"]] == [
        "write_file"
    ]


def test_active_catalog_is_bounded_and_reports_eviction():
    tools = [
        _tool(f"task_tool_{index:02d}", "Handle a specialized task", deferred=True)
        for index in range(14)
    ]
    active = ["task_tool_00", "task_tool_01"]

    result = make_tool_search_tool(tools, active).call(
        {"query": "task", "max_results": 20},
        ToolRuntime(tool_name="tool_search"),
    )

    assert result.data["evicted"] == ["task_tool_00", "task_tool_01"]
    assert len(result.data["activated"]) == 12
    assert active == [f"task_tool_{index:02d}" for index in range(2, 14)]
    assert result.data["metrics"]["active_count"] == 12


def test_search_reports_baseline_tools_instead_of_activating_lookalikes():
    directory = _tool("list_directory", "List the immediate children of a directory")
    schedules = _tool(
        "list_schedules", "List durable schedules", deferred=True
    )
    result = make_tool_search_tool([directory, schedules], []).call(
        {"query": "list directory"}, ToolRuntime(tool_name="tool_search")
    )

    assert result.data["already_available"] == ["list_directory"]
    assert result.data["activated"] == []


def test_search_does_not_activate_unrelated_list_tools():
    result = make_tool_search_tool(
        [_tool("list_schedules", "List durable schedules", deferred=True)],
        [],
    ).call(
        {"query": "read write edit files list directory"},
        ToolRuntime(tool_name="tool_search"),
    )

    assert result.data["activated"] == []


def test_encode_tools_without_a_dynamic_catalog_includes_declared_tools():
    deferred = _tool("special", "Specialized", deferred=True)
    schemas, _ = encode_tools([deferred])
    assert schemas[0]["name"] == "special"


def test_builtin_capabilities_use_hybrid_loading():
    """Coding, plan, and web tools stay on every request."""
    assert built_in_tools
    by_name = {tool.name: tool for tool in built_in_tools}
    core = {
        "list_directory",
        "glob",
        "grep",
        "read_file",
        "write_file",
        "edit_file",
        "execute_command",
        "create_plan",
        "update_plan",
        "get_plan",
        "replan",
        "web_search",
        "http_request",
    }

    assert core <= by_name.keys()
    assert all(not tool.defer_to_model for tool in built_in_tools)

    schemas, _ = encode_tools(built_in_tools, active_deferred=set())
    assert {
        item["name"] for item in schemas
    } == core


def test_web_memory_and_spawn_are_baseline():
    from ..subagent import build_agent_tools
    from ..tools.memory_tools import build_memory_tools

    class UnusedLLM:
        context_limit = 128_000

    tools = [
        *build_agent_tools(
            UnusedLLM(),
            list(built_in_tools),
            depth=0,
            max_depth=1,
            enable_autonomy=True,
        ),
        *build_memory_tools(),
    ]
    by_name = {tool.name: tool for tool in tools}
    baseline = {
        "web_search",
        "http_request",
        "spawn_agent",
        "get_agent_tree",
        "get_task",
        "wait_task",
        "cancel_task",
        "list_tasks",
        "create_memory",
        "get_memory",
        "update_memory",
        "delete_memory",
        "search_memory",
    }
    deferred = {
        "schedule_task",
        "get_schedule",
        "list_schedules",
        "pause_schedule",
        "resume_schedule",
        "cancel_schedule",
        "list_task_runs",
    }
    assert all(not by_name[name].defer_to_model for name in baseline)
    assert all(by_name[name].defer_to_model for name in deferred)


def test_agent_refreshes_schemas_after_tool_search(tmp_path):
    specialized = _tool(
        "schedule_task", "Create a durable recurring scheduled task", deferred=True
    )

    class ScriptLLM:
        context_limit = 128_000

        def __init__(self):
            self.script = [
                response(calls=[{
                    "name": "tool_search",
                    "arguments": {"query": "schedule recurring task"},
                }]),
                response(calls=[{"name": "schedule_task", "arguments": {}}]),
                response(content="done"),
            ]
            self.schema_names: list[list[str]] = []

        def __call__(self, messages, *, tools):
            self.schema_names.append([
                    item["name"] for item in tools
            ])
            yield event(self.script.pop(0))

    llm = ScriptLLM()
    agent = Agent(
        llm,
        [specialized],
        Session.create("activate", tmp_path),
        SilentRenderer(),
    )

    assert agent.run("schedule it") == "done"
    assert llm.schema_names[0] == ["tool_search"]
    assert llm.schema_names[1] == ["schedule_task", "tool_search"]


def test_agent_restores_active_deferred_tools_from_session(tmp_path):
    specialized = _tool(
        "schedule_task", "Create a durable recurring scheduled task", deferred=True
    )
    session = Session.create("resume", tmp_path)
    session.active_deferred_tools = ["missing_tool", "schedule_task"]

    class UnusedLLM:
        context_limit = 128_000

        def __call__(self, messages, *, tools):
            raise AssertionError("LLM should not be called")

    agent = Agent(UnusedLLM(), [specialized], session, SilentRenderer())

    assert session.active_deferred_tools == ["schedule_task"]
    assert [item["name"] for item in agent.tool_schemas] == [
        "schedule_task",
        "tool_search",
    ]


def test_agent_rejects_deferred_tool_before_activation(tmp_path):
    invoked: list[str] = []
    specialized = Tool(
        name="schedule_task",
        description="Create a durable recurring scheduled task",
        parameters={"type": "object", "properties": {}},
        call=lambda args, runtime: invoked.append("schedule_task") or ToolResult.success(),
        defer_to_model=True,
    )

    class ScriptLLM:
        context_limit = 128_000

        def __init__(self):
            self.script = [
                response(calls=[{"name": "schedule_task", "arguments": {}}]),
                response(content="done"),
            ]

        def __call__(self, messages, *, tools):
            yield event(self.script.pop(0))

    answer = Agent(
        ScriptLLM(),
        [specialized],
        Session.create("must-search", tmp_path),
        SilentRenderer(),
    ).run("schedule it")

    assert answer == "done"
    assert invoked == []
