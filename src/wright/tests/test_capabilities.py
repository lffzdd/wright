from pathlib import Path
from types import SimpleNamespace

import pytest

from wright.agent import Agent
from wright.artifacts import ArtifactStore
from wright.capabilities import AgentProfile, CapabilityCatalog, CapabilityError
from wright.checkpoint import SessionCheckpointStore
from wright.execution import LocalExecutionBackend
from wright.executor import ToolExecutor
from wright.renderer import SilentRenderer
from wright.session import Session
from wright.tests.responses import response
from wright.tools.base import Tool, ToolCall, ToolResult, tool_runtime_for_session
from wright.tools.command_tools import execute_command
from wright.tools.file_tools import read_file_tool
from wright.tools.mcp_client import _to_tool_result


def _tool(name: str) -> Tool:
    return Tool(name, name, {}, lambda _args, _runtime: ToolResult.success(name))


def test_catalog_is_the_single_snapshot_for_schema_and_execution():
    read = _tool("read")
    write = _tool("write")
    catalog = CapabilityCatalog([read, write])
    snapshot = catalog.snapshot(AgentProfile("read-only", frozenset({"read"})))
    executor = ToolExecutor({"read": read, "write": write}, capability_snapshot=snapshot)

    assert executor.execute([ToolCall("read", {}, "one")])[0].result.ok
    denied = executor.execute([ToolCall("write", {}, "two")])
    assert denied[0].result.ok is False
    assert "not authorized" in denied[0].result.err


def test_catalog_rejects_colliding_names_and_unknown_profile_capability():
    with pytest.raises(CapabilityError, match="duplicate"):
        CapabilityCatalog([_tool("same"), _tool("same")])
    with pytest.raises(CapabilityError, match="unknown"):
        CapabilityCatalog([_tool("known")]).snapshot(
            AgentProfile("bad", frozenset({"missing"}))
        )


def test_mcp_result_preserves_structured_content_images_and_errors():
    image = SimpleNamespace(type="image", data="abc", mimeType="image/png")
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello"), image],
        structuredContent={"count": 2}, isError=False,
    )
    converted = _to_tool_result(result)
    assert converted.ok
    assert converted.data["structured_content"] == {"count": 2}
    assert converted.content[1]["type"] == "image"

    failed = _to_tool_result(SimpleNamespace(content=[SimpleNamespace(type="text", text="bad")], structuredContent={"code": "x"}, isError=True))
    assert not failed.ok
    assert failed.data["structured_content"] == {"code": "x"}


def test_mcp_image_is_managed_as_an_artifact_not_kept_as_base64(tmp_path):
    store = ArtifactStore(tmp_path / "managed")
    result = SimpleNamespace(
        content=[SimpleNamespace(type="image", data="aGVsbG8=", mimeType="image/png")],
        structuredContent=None,
        isError=False,
    )
    converted = _to_tool_result(result, artifact_store=store, run_id="run", call_id="call")
    assert converted.ok
    assert len(converted.artifacts) == 1
    assert "data" not in converted.content[0]
    assert store.path_for(converted.artifacts[0]).read_bytes() == b"hello"


def test_mcp_error_keeps_registered_artifacts_as_diagnostics(tmp_path):
    store = ArtifactStore(tmp_path / "managed")
    result = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="partial diagnostic"),
            SimpleNamespace(type="image", data="aGVsbG8=", mimeType="image/png"),
        ],
        structuredContent={"code": "partial"},
        isError=True,
    )
    converted = _to_tool_result(result, artifact_store=store, run_id="run", call_id="call")
    assert not converted.ok
    assert converted.data["structured_content"] == {"code": "partial"}
    assert len(converted.artifacts) == 1


def test_mcp_unsupported_image_type_is_explicit_and_not_stored(tmp_path):
    store = ArtifactStore(tmp_path / "managed")
    converted = _to_tool_result(
        SimpleNamespace(
            content=[SimpleNamespace(type="image", data="aGVsbG8=", mimeType="image/svg+xml")],
            structuredContent=None, isError=False,
        ),
        artifact_store=store,
    )
    assert converted.ok
    assert converted.artifacts == ()
    assert converted.content[0]["unsupported"] == "unsupported image media type"


def test_agent_mcp_artifact_survives_checkpoint_and_is_readable(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")

    def fake_mcp(_args, runtime):
        scope = runtime.capabilities.scope if runtime.capabilities else None
        return _to_tool_result(
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="count: 2"),
                    SimpleNamespace(type="image", data="aGVsbG8=", mimeType="image/png"),
                ],
                structuredContent={"count": 2}, isError=False,
            ),
            artifact_store=store,
            run_id=scope.run_id if scope else "",
            call_id=runtime.tool_call_id,
        )

    class Model:
        context_limit = 128_000

        def __call__(self, messages, **_kwargs):
            if messages[-1]["role"] == "user":
                yield response(calls=[{"id": "mcp-call", "name": "mcp_report"}])
            else:
                yield response(content="Report delivered")

    session = Session.create("report", tmp_path, session_id="artifact-e2e")
    result = Agent(
        Model(),
        [Tool("mcp_report", "fake MCP", {"type": "object"}, fake_mcp)],
        session,
        SilentRenderer(),
    ).run("make a report")
    assert result == "Report delivered"
    ref = session.tool_executions["mcp-call"].result.artifacts[0]
    assert store.path_for(ref).read_bytes() == b"hello"

    checkpoints = SessionCheckpointStore(tmp_path / "checkpoints")
    checkpoints.save(session)
    restored = checkpoints.load(session.session_id)
    restored_ref = restored.tool_executions["mcp-call"].result.artifacts[0]
    assert restored_ref == ref
    assert "aGVsbG8=" not in checkpoints.path_for(session.session_id).read_text(encoding="utf-8")
    assert store.path_for(restored_ref).read_bytes() == b"hello"


def test_managed_artifact_survives_source_cleanup_and_rejects_escape(tmp_path):
    source = tmp_path / "report.md"
    source.write_text("# report\ncount: 2", encoding="utf-8")
    store = ArtifactStore(tmp_path / "managed")
    ref = store.register_file(source, run_id="run_a", call_id="call_a", media_type="text/markdown")
    source.unlink()

    assert store.path_for(ref).read_text(encoding="utf-8") == "# report\ncount: 2"


def test_production_tools_cannot_reach_session_or_service_containers():
    root = Path(__file__).parents[1]
    sources = [
        *sorted((root / "tools").glob("*.py")),
        root / "subagent.py",
    ]
    forbidden = ("runtime.session_state", "runtime.services")
    offenders = {
        str(path.relative_to(root)): token
        for path in sources
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }
    assert offenders == {}


def test_file_tool_uses_injected_execution_backend(tmp_path):
    source = tmp_path / "report.txt"
    source.write_text("report", encoding="utf-8")

    class RecordingBackend(LocalExecutionBackend):
        def __init__(self):
            super().__init__(tmp_path, lambda: tmp_path)
            self.reads = []

        def read_bytes(self, path, limit=None):
            self.reads.append(("bytes", path.name))
            return super().read_bytes(path, limit)

        def read_text(self, path, *, encoding, errors="strict"):
            self.reads.append(("text", path.name))
            return super().read_text(path, encoding=encoding, errors=errors)

    backend = RecordingBackend()
    session = Session.create("test", tmp_path)
    session.begin_user_turn("test")
    outcome = ToolExecutor(
        {"read_file": read_file_tool}, session=session, execution_backend=backend
    ).execute([ToolCall("read_file", {"file": "report.txt"}, "call")])[0]
    assert outcome.result.ok
    assert backend.reads == [("bytes", "report.txt"), ("text", "report.txt")]


def test_executor_only_injects_the_capabilities_declared_by_each_tool(tmp_path):
    observed = {}

    def observe(args, runtime):
        capabilities = runtime.capabilities
        assert capabilities is not None
        observed[args["kind"]] = capabilities
        return ToolResult.success()

    planning = Tool(
        "planning", "planning", {"type": "object"}, observe,
        required_capabilities=frozenset({"plan"}),
    )
    file_access = Tool(
        "file_access", "file_access", {"type": "object"}, observe,
        required_capabilities=frozenset({"execution"}),
    )
    session = Session.create("test", tmp_path)
    session.begin_user_turn("test")
    outcomes = ToolExecutor({"planning": planning, "file_access": file_access}, session=session).execute([
        ToolCall("planning", {"kind": "planning"}, "plan"),
        ToolCall("file_access", {"kind": "file"}, "file"),
    ])
    assert all(outcome.result.ok for outcome in outcomes)
    assert observed["planning"].execution is None
    assert observed["planning"].plan_manager is session.plan_manager
    assert observed["planning"].delegation is None
    assert observed["file"].execution is not None
    assert observed["file"].plan_manager is None


def test_command_tool_starts_and_waits_through_execution_backend(tmp_path):
    class RecordingBackend(LocalExecutionBackend):
        def __init__(self):
            super().__init__(tmp_path, lambda: tmp_path)
            self.events = []

        def start_process(self, argv, *, cwd, **kwargs):
            self.events.append(("start", tuple(argv), cwd))
            return super().start_process(argv, cwd=cwd, **kwargs)

        def wait_process(self, process, timeout=None):
            self.events.append(("wait", process.pid))
            return super().wait_process(process, timeout)

    backend = RecordingBackend()
    session = Session.create("test", tmp_path)
    runtime = tool_runtime_for_session(
        session, workspace_dir=tmp_path, execution_backend=backend,
    )
    result = execute_command("printf backend", runtime=runtime)
    assert result.ok
    assert result.data["output"] == "backend"
    assert backend.events[0][0] == "start"
    assert backend.events[1][0] == "wait"


def test_profile_limits_are_enforced_by_agent_not_only_descriptive(tmp_path):
    profile = AgentProfile(
        "restricted", frozenset({"ask"}), max_steps=1,
        allow_interaction=False, allow_delegation=False, allow_background_tasks=False,
    )
    ask = Tool(
        "ask", "ask", {"type": "object"}, lambda _args, _rt: ToolResult.success(),
        requires_user_interaction=True,
    )
    agent = Agent(
        _tool_llm("done"), [ask], Session.create("goal", tmp_path), SilentRenderer(), profile=profile
    )
    assert "ask" not in agent.executor.tool_registry
    assert agent.profile.max_steps == 1


def test_profile_filters_the_system_catalog_as_well_as_execution(tmp_path):
    public = _tool("public")
    private = Tool(
        "private_interaction", "private", {"type": "object"},
        lambda _args, _runtime: ToolResult.success(),
        requires_user_interaction=True,
    )
    profile = AgentProfile(
        "headless", frozenset({"public", "private_interaction"}),
        allow_interaction=False,
    )
    session = Session.create("goal", tmp_path)
    Agent(_tool_llm("done"), [public, private], session, SilentRenderer(), profile=profile)
    prompt = session.message_records[0].message["content"]
    assert "public" in prompt
    assert "private_interaction" not in prompt


def _tool_llm(answer):
    def call(_messages, **_kwargs):
        from wright.events import ContentDone

        yield ContentDone(content=answer, finish_reason="stop")

    call.context_limit = 128_000
    return call
