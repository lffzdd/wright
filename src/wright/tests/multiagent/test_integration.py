import json
import time

from ...agent import Agent
from ...checkpoint import SessionCheckpointStore
from ...coordination import AgentControlConfig, AgentControlPlane
from ...events import ContentDone, UsageEvent
from ...executor import ToolExecutor
from ...renderer import SilentRenderer
from ...session import SessionState
from ...subagent import build_agent_tools, make_spawn_agent_tool
from ...tools.base import ToolCall, ToolRuntime
from ...tools.command_tools import execute_command


def _tool(name, **arguments):
    return json.dumps({
        "tool_calls": [{"name": name, "arguments": arguments}],
        "final_answer": None,
    })


def _final(answer):
    return json.dumps({"tool_calls": [], "final_answer": answer})


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, script):
        self.script = list(script)

    def __call__(self, messages):
        yield ContentDone(self.script.pop(0))


def test_nested_agents_form_one_shared_task_tree(tmp_path):
    llm = ScriptLLM([
        _tool("spawn_agent", task="outer"),
        _tool("spawn_agent", task="inner"),
        _final("inner done"),
        _final("outer done"),
        _final("root done"),
    ])
    session = SessionState.create("root", tmp_path)
    tools = build_agent_tools(
        llm, [], max_depth=2, render_subagents=False
    )

    result = Agent(llm, tools, session, SilentRenderer()).run("root task")

    assert result == "root done"
    tree = session.control_plane.tree(session.agent_root_turn_id)
    assert len(tree) == 1
    assert tree[0]["task"] == "outer"
    assert tree[0]["status"] == "completed"
    assert tree[0]["children"][0]["task"] == "inner"
    assert tree[0]["children"][0]["status"] == "completed"


class _Usage:
    prompt_tokens = 8
    completion_tokens = 5
    total_tokens = 13


class UsageLLM:
    context_limit = 128_000

    def __call__(self, messages):
        yield UsageEvent(_Usage())
        yield ContentDone(_final("would finish"))


def test_shared_token_budget_stops_child_before_accepting_final(tmp_path):
    session = SessionState.create("root", tmp_path)
    session.control_plane = AgentControlPlane(
        AgentControlConfig(max_tokens_per_turn=10)
    )
    session.begin_user_turn("root")
    spawn = make_spawn_agent_tool(
        UsageLLM(), [], max_depth=1, render_subagents=False
    )
    runtime = ToolRuntime(
        tool_name="spawn_agent",
        tool_call_id="call_1",
        workspace_dir=tmp_path,
        cwd_provider=session.get_cwd,
        session_state=session,
    )

    result = spawn.call({"task": "expensive"}, runtime)

    assert not result.ok
    assert result.data["task_status"] == "cancelled"
    task = session.control_plane.get(result.data["task_id"])
    assert task.total_tokens == 13
    assert "token" in task.cancel_reason


class SlowFinalLLM:
    context_limit = 128_000

    def __call__(self, messages):
        time.sleep(0.05)
        yield ContentDone(_final("too late"))


def test_executor_deadline_propagates_to_child_control_state(tmp_path):
    session = SessionState.create("root", tmp_path)
    session.begin_user_turn("root")
    spawn = make_spawn_agent_tool(
        SlowFinalLLM(), [], max_depth=1, child_timeout=0.01,
        render_subagents=False
    )
    executor = ToolExecutor(
        {"spawn_agent": spawn},
        tool_timeout=0.01,
        session_state=session,
    )

    outcome = executor.execute([
        ToolCall("spawn_agent", {"task": "slow"}, "call_1")
    ])[0]

    assert outcome.status == "timeout"
    assert outcome.result.data["task_id"]
    tree = session.control_plane.tree(session.agent_root_turn_id)
    assert tree[0]["status"] == "timed_out"


def test_control_plane_changes_are_checkpointed_and_live_tasks_recover_unknown(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionState.create("root", workspace)
    session.begin_user_turn("root")
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    Agent(
        ScriptLLM([_final("unused")]),
        [],
        session,
        SilentRenderer(),
        checkpoint_store=store,
    )

    running = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=None,
        tool_call_id="call_1",
        depth=1,
        task="in flight",
        requested_steps=5,
    )
    restored = store.load(session.session_id)

    recovered = restored.control_plane.get(running.id)
    assert recovered.status == "failed"
    assert "结果未知" in recovered.error


def test_spawn_emits_structured_lifecycle_events(tmp_path):
    events = []
    session = SessionState.create("root", tmp_path)
    session.begin_user_turn("root")
    spawn = make_spawn_agent_tool(
        ScriptLLM([_final("done")]),
        [],
        max_depth=1,
        render_subagents=False,
    )
    runtime = ToolRuntime(
        tool_name="spawn_agent",
        tool_call_id="call_1",
        workspace_dir=tmp_path,
        cwd_provider=session.get_cwd,
        session_state=session,
        emit_progress=events.append,
    )

    result = spawn.call({"task": "observable"}, runtime)

    assert result.ok
    assert [event["status"] for event in events] == ["running", "completed"]
    assert events[0]["task_id"] == result.data["task_id"]


def test_child_runtime_cannot_leave_background_processes(tmp_path):
    session = SessionState.create("child", tmp_path)
    runtime = ToolRuntime(
        tool_name="execute_command",
        workspace_dir=tmp_path,
        cwd_provider=session.get_cwd,
        session_state=session,
        allow_background_tasks=False,
    )

    immediate = execute_command(
        "sleep 1", run_in_background=True, runtime=runtime
    )
    timed = execute_command("sleep 0.1", timeout=0, runtime=runtime)

    assert not immediate.ok
    assert not timed.ok
    assert session.background_tasks == {}
