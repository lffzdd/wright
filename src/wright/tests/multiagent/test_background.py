import json
import queue
import time

from ...agent_background import AgentBackgroundRuntime
from ...events import ContentDone
from ...renderer import SilentRenderer
from ...session import SessionState
from ...subagent import (
    cancel_agent_task_tool,
    get_agent_task_tool,
    make_spawn_agent_tool,
)
from ...tools.base import ToolRuntime


def _final(answer: str) -> str:
    return json.dumps({"tool_calls": [], "final_answer": answer})


class SlowFinalLLM:
    context_limit = 128_000

    def __call__(self, messages):
        time.sleep(0.05)
        yield ContentDone(_final("background done"))


def _runtime(session, tmp_path):
    return ToolRuntime(
        tool_name="spawn_agent",
        tool_call_id="call_1",
        workspace_dir=tmp_path,
        cwd_provider=session.get_cwd,
        session_state=session,
    )


def test_background_agent_returns_immediately_and_notifies_once(tmp_path):
    events = queue.Queue()
    background = AgentBackgroundRuntime(events, max_workers=1)
    session = SessionState.create("root", tmp_path)
    session.begin_user_turn("root")
    session.agent_background_runtime = background
    spawn = make_spawn_agent_tool(
        SlowFinalLLM(), [], max_depth=1, render_subagents=False
    )

    started = time.monotonic()
    launched = spawn.call(
        {"task": "background work", "run_in_background": True},
        _runtime(session, tmp_path),
    )

    assert launched.ok
    assert launched.data["task_status"] == "async_launched"
    assert time.monotonic() - started < 0.04
    event_type, task_id = events.get(timeout=1)
    assert event_type == "TASK_DONE"
    assert task_id == launched.data["task_id"]
    record = session.control_plane.get(task_id)
    assert record.status == "completed"
    assert record.result == "background done"
    assert events.empty()
    background.shutdown(session.control_plane)


def test_get_agent_task_reads_background_terminal_record(tmp_path):
    events = queue.Queue()
    background = AgentBackgroundRuntime(events, max_workers=1)
    session = SessionState.create("root", tmp_path)
    session.begin_user_turn("root")
    session.agent_background_runtime = background
    spawn = make_spawn_agent_tool(
        SlowFinalLLM(), [], max_depth=1, render_subagents=False
    )
    launched = spawn.call(
        {"task": "background work", "run_in_background": True},
        _runtime(session, tmp_path),
    )
    events.get(timeout=1)

    result = get_agent_task_tool.call(
        {"task_id": launched.data["task_id"]},
        ToolRuntime(workspace_dir=tmp_path, session_state=session),
    )
    assert result.ok
    assert result.data["status"] == "completed"
    assert result.data["result"] == "background done"
    unknown = get_agent_task_tool.call(
        {"task_id": "missing"}, ToolRuntime(session_state=session)
    )
    assert not unknown.ok
    background.shutdown(session.control_plane)


def test_child_agent_cannot_launch_background_agent(tmp_path):
    session = SessionState.create("child", tmp_path)
    session.agent_task_id = "parent"
    session.begin_user_turn("child")
    session.agent_background_runtime = AgentBackgroundRuntime(queue.Queue())
    spawn = make_spawn_agent_tool(
        SlowFinalLLM(), [], max_depth=2, render_subagents=False
    )
    result = spawn.call(
        {"task": "forbidden", "run_in_background": True},
        _runtime(session, tmp_path),
    )
    assert not result.ok
    session.agent_background_runtime.shutdown(session.control_plane)


def test_cancel_agent_task_requests_cooperative_cancellation(tmp_path):
    session = SessionState.create("root", tmp_path)
    session.begin_user_turn("root")
    record = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=None,
        tool_call_id="call_1",
        depth=1,
        task="long work",
        requested_steps=5,
    )

    result = cancel_agent_task_tool.call(
        {"task_id": record.id, "reason": "no longer needed"},
        ToolRuntime(session_state=session),
    )

    assert result.ok
    assert result.data["cancel_requested"] is True
    assert session.control_plane.is_cancelled(record.id)
    assert session.control_plane.cancellation_reason(record.id) == "no longer needed"
