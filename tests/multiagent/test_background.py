import queue
import threading
import time

from tests.responses import event, response
from wright.application.agent import AgentBackgroundRuntime, make_spawn_agent_tool
from wright.application.composition.services import RuntimeServices
from wright.application.tool_execution.runtime import tool_runtime_for_session
from wright.domain.model.llm.events import ContentDone
from wright.domain.model.session import Session
from wright.infrastructure.tools.agent_tools import cancel_agent_tool, get_agent_tool


def _final(answer: str) -> ContentDone:
    return response(content=answer, calls=[])


class SlowFinalLLM:
    context_limit = 128_000

    def __call__(self, messages, **kwargs):
        time.sleep(0.05)
        yield event(_final("background done"))


def _runtime(session, tmp_path, background=None):
    return tool_runtime_for_session(
        session,
        tool_name="spawn_agent",
        tool_call_id="call_1",
        workspace_dir=tmp_path,
        cwd_provider=session.get_cwd,
        services=RuntimeServices(agent_background=background),
    )


def test_background_agent_returns_immediately_and_notifies_once(tmp_path):
    events = queue.Queue()
    background = AgentBackgroundRuntime(events, max_workers=1)
    session = Session.create("root", tmp_path)
    session.begin_user_turn("root")
    release = threading.Event()
    entered = threading.Event()
    results = queue.Queue()

    class GatedLLM:
        context_limit = 128_000

        def __call__(self, messages, **kwargs):
            entered.set()
            assert release.wait(timeout=10)
            yield event(_final("background done"))

    spawn = make_spawn_agent_tool(GatedLLM(), [], max_depth=1, render_subagents=False)
    runtime = _runtime(session, tmp_path, background)

    def submit():
        results.put(
            spawn.call({"task": "background work", "run_in_background": True}, runtime)
        )

    submitter = threading.Thread(target=submit, daemon=True)
    try:
        submitter.start()
        launched = results.get(timeout=5)
        assert entered.wait(timeout=5)
        assert launched.ok
        assert launched.data["status"] == "running"
        assert launched.data["agent_task_id"]
        assert "task_id" not in launched.data
        assert not release.is_set()
        assert events.empty()
        release.set()
        event_type, task_id = events.get(timeout=5)
        assert event_type == "TASK_DONE"
        assert task_id == launched.data["agent_task_id"]
        record = session.control_plane.get(task_id)
        assert record.status == "completed"
        assert record.result == "background done"
        assert events.empty()
    finally:
        release.set()
        submitter.join(timeout=5)
        background.shutdown(session.control_plane)


def test_get_agent_reads_background_terminal_record(tmp_path):
    events = queue.Queue()
    background = AgentBackgroundRuntime(events, max_workers=1)
    session = Session.create("root", tmp_path)
    session.begin_user_turn("root")
    spawn = make_spawn_agent_tool(
        SlowFinalLLM(), [], max_depth=1, render_subagents=False
    )
    launched = spawn.call(
        {"task": "background work", "run_in_background": True},
        _runtime(session, tmp_path, background),
    )
    events.get(timeout=1)

    result = get_agent_tool.call(
        {"agent_task_id": launched.data["agent_task_id"]},
        tool_runtime_for_session(session, workspace_dir=tmp_path),
    )
    assert result.ok
    assert result.data["status"] == "completed"
    assert result.data["result"] == "background done"
    assert result.data["agent_task_id"] == launched.data["agent_task_id"]
    unknown = get_agent_tool.call(
        {"agent_task_id": "missing"}, tool_runtime_for_session(session)
    )
    assert not unknown.ok
    background.shutdown(session.control_plane)


def test_child_agent_cannot_launch_background_agent(tmp_path):
    session = Session.create("child", tmp_path)
    session.agent_task_id = "parent"
    session.begin_user_turn("child")
    background = AgentBackgroundRuntime(queue.Queue())
    spawn = make_spawn_agent_tool(
        SlowFinalLLM(), [], max_depth=2, render_subagents=False
    )
    result = spawn.call(
        {"task": "forbidden", "run_in_background": True},
        _runtime(session, tmp_path, background),
    )
    assert not result.ok
    background.shutdown(session.control_plane)


def test_cancel_agent_requests_cooperative_cancellation(tmp_path):
    session = Session.create("root", tmp_path)
    session.begin_user_turn("root")
    record = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=None,
        tool_call_id="call_1",
        depth=1,
        task="long work",
        requested_steps=5,
    )

    result = cancel_agent_tool.call(
        {"agent_task_id": record.id, "reason": "no longer needed"},
        tool_runtime_for_session(session),
    )

    assert result.ok
    assert result.data["status"] == "running"
    assert result.data["cancel_requested"] is True
    assert "has not stopped" in result.data["message"]
    assert session.control_plane.is_cancelled(record.id)
    assert session.control_plane.cancellation_reason(record.id) == "no longer needed"
