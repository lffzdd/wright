import json
import queue
import threading
from types import SimpleNamespace

import pytest

from tests.responses import event, response
from wright.application.composition.agent_factory import create_agent
from wright.application.session.service import SessionService, SessionServiceError
from wright.application.session_runtime.publisher import EventPublisher
from wright.application.tool_execution.tool_search import make_tool_search_tool
from wright.application.workspace.mcp_servers import project_mcp_servers
from wright.domain.model.mcp import mcp_server_of, mcp_tool_enabled
from wright.domain.model.session import Session, SessionAuthorization
from wright.domain.model.tool import ToolResult
from wright.domain.model.tool_definition import Tool
from wright.domain.model.tool_runtime import ToolRuntime
from wright.infrastructure.tools.mcp_client import _failed_status
from wright.interfaces.interaction import InteractionBroker

SERVERS = [
    {"name": "github", "status": "connected", "tools": ["mcp__github__search", "mcp__github__issue"], "error": None},
    {"name": "browser", "status": "auth_required", "tools": [], "error": "HTTPStatusError: 401 Unauthorized"},
]


def _tool(name: str, *, deferred: bool = True, calls: list | None = None) -> Tool:
    def call(args, runtime):
        if calls is not None:
            calls.append(name)
        return ToolResult.success()

    return Tool(
        name=name,
        description=f"{name} search issues",
        parameters={"type": "object", "properties": {}},
        call=call,
        defer_to_model=deferred,
    )


def test_tool_names_map_to_their_server():
    assert mcp_server_of("mcp__github__search") == "github"
    assert mcp_server_of("read_file") is None
    assert mcp_server_of("mcp__broken") is None
    assert not mcp_tool_enabled("mcp__github__search", ["github"])
    assert mcp_tool_enabled("mcp__other__search", ["github"])
    assert mcp_tool_enabled("read_file", ["github"])


def test_failed_connections_say_whether_signing_in_again_would_help():
    assert _failed_status(RuntimeError("401 Unauthorized"))["status"] == "auth_required"
    failed = _failed_status(FileNotFoundError("npx"))
    assert failed["status"] == "failed"
    assert failed["error"] == "FileNotFoundError: npx"


def test_server_rows_count_tools_and_only_the_schemas_the_next_request_sends():
    schemas = [{"name": "mcp__github__search", "description": "x" * 400, "parameters": {}}]
    rows = project_mcp_servers(SERVERS, [], schemas)
    assert [(row["name"], row["status"], row["tool_count"], row["toggleable"]) for row in rows] == [
        ("github", "connected", 2, True),
        ("browser", "auth_required", 0, False),
    ]
    assert rows[0]["tokens"] > 0
    assert rows[1]["error"] == "HTTPStatusError: 401 Unauthorized"
    assert project_mcp_servers(SERVERS, [], [])[0]["tokens"] == 0


def test_a_turned_off_server_shows_as_disabled_and_still_counts_its_stubs():
    schemas = [{"name": "mcp__github__search", "description": "x", "parameters": {}}]
    row = project_mcp_servers(SERVERS, ["github"], schemas)[0]
    # Its stubs stay in the schema list so the prompt cache survives the toggle.
    assert (row["status"], row["toggleable"]) == ("disabled", True)
    assert row["tokens"] > 0


def test_aliased_schema_names_still_count_for_their_server():
    alias = "mcp__github__search_0123456789abcdef0123"
    schemas = [{"name": alias, "description": "x", "parameters": {}}]
    rows = project_mcp_servers(SERVERS, [], schemas, {alias: "mcp__github__search.v2"})
    assert rows[0]["tokens"] > 0


def test_tool_search_skips_tools_of_a_server_turned_off_after_it_was_built():
    disabled: list[str] = []
    tool = _tool("mcp__github__search")
    search = make_tool_search_tool([tool], lambda name: mcp_tool_enabled(name, disabled))
    runtime = ToolRuntime(tool_name="tool_search")
    assert search.call({"query": "github search issues"}, runtime).data["tools"]
    disabled.append("github")
    assert not search.call({"query": "github search issues"}, runtime).data["tools"]


def test_turning_a_server_off_keeps_tools_and_refuses_its_calls(tmp_path):
    calls: list[str] = []
    session = Session.create("mcp", tmp_path)

    class ScriptLLM:
        context_limit = 128_000

        def __init__(self):
            self.script = [
                response(content="first"),
                response(calls=[{"name": "mcp__github__search", "arguments": {}}]),
                response(content="done"),
            ]
            self.tool_payloads: list[str] = []
            self.requests: list = []

        def __call__(self, messages, *, tools):
            self.tool_payloads.append(json.dumps(list(tools), sort_keys=True))
            self.requests.append(list(messages))
            yield event(self.script.pop(0))

    llm = ScriptLLM()
    agent = create_agent(llm, [_tool("mcp__github__search", calls=calls)], session)
    assert agent.run("hello") == "first"
    session.disabled_mcp_servers.append("github")
    assert agent.run("search") == "done"

    # The schema list is byte-identical before and after the toggle.
    assert len(set(llm.tool_payloads)) == 1
    assert "mcp__github__search" in llm.tool_payloads[0]
    assert "<mcp-servers-off>" not in json.dumps(llm.requests[0])
    notice = llm.requests[1][-1]["content"]
    assert "<mcp-servers-off>" in notice and "github" in notice
    # A call the model makes anyway is refused before it reaches the server.
    assert calls == []
    assert any(
        "'github' is turned off" in str(message.get("content"))
        for message in session.wire_messages()
    )


def _runtime(*, store=None):
    publisher = EventPublisher(project_id="project", session_id="session")
    idle = threading.Event()
    idle.set()
    state = SimpleNamespace(
        session_id="session", authorization=SessionAuthorization("session"), status="completed",
        model_name="m", user_goal="", environment="local", execution_mode="sandbox",
        workspace_dir=".", project_root=".", base_commit=None, branch_name=None, llm_transport="chat",
        lifecycle="open", attachment_records=lambda ids: [],
        disabled_mcp_servers=[],
    )
    llm = SimpleNamespace(model="m")
    return SimpleNamespace(
        session_state=state, publisher=publisher, interaction_broker=InteractionBroker(publisher),
        event_queue=queue.Queue(), agent_idle=idle, cancellation_event=threading.Event(), llm=llm,
        agent=SimpleNamespace(llm=llm, checkpoint_store=store, continue_run=lambda: None, tool_schemas=[]),
        mcp_manager=SimpleNamespace(servers=lambda: SERVERS), resumed=False,
    )


def test_turning_a_server_off_saves_and_tells_the_page():
    saved = []
    runtime = _runtime(store=SimpleNamespace(save=lambda state: saved.append(list(state.disabled_mcp_servers))))
    service = SessionService(runtime, shutdown=lambda _: None)
    _subscriber, inbox = runtime.publisher.subscribe()

    rows = service.set_mcp_server("github", False)
    assert rows[0]["status"] == "disabled"
    assert saved == [["github"]]
    published = []
    while not inbox.empty():
        published.append(inbox.get_nowait())
    assert any(item.type == "session.mcp_updated" and item.payload["mcp_servers"] == rows for item in published)

    assert service.set_mcp_server("github", True)[0]["status"] == "connected"
    assert runtime.session_state.disabled_mcp_servers == []


def test_server_switch_is_refused_while_running_for_unknown_or_unconnected_servers_and_rolls_back():
    runtime = _runtime()
    service = SessionService(runtime, shutdown=lambda _: None)
    with pytest.raises(SessionServiceError, match="unknown MCP server"):
        service.set_mcp_server("nope", False)
    with pytest.raises(SessionServiceError, match="not connected"):
        service.set_mcp_server("browser", True)
    runtime.agent_idle.clear()
    with pytest.raises(SessionServiceError, match="still executing"):
        service.set_mcp_server("github", False)
    runtime.agent_idle.set()

    def broken(_state):
        raise OSError("disk full")

    runtime.agent.checkpoint_store = SimpleNamespace(save=broken)
    with pytest.raises(SessionServiceError, match="checkpoint failed"):
        service.set_mcp_server("github", False)
    assert runtime.session_state.disabled_mcp_servers == []
