import json

import pytest

from wright.tests.responses import event, response

from ...agent import Agent
from ...lifecycle import HookRegistration, LifecycleManager, TraceRecorder
from ...renderer import SilentRenderer
from ...session import Session
from ...subagent import make_spawn_agent_tool
from ...tools.base import Tool, ToolResult, tool_runtime_for_session


def _final(answer):
    return response(content=answer, calls=[])


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, script):
        self.script = list(script)

    def __call__(self, messages, **kwargs):
        yield event(self.script.pop(0))


class BrokenLLM:
    context_limit = 128_000

    def __call__(self, messages, **kwargs):
        raise RuntimeError("provider unavailable")
        yield


class RecordingMemory:
    def __init__(self):
        self.finalized = []

    def instructions(self):
        return ""

    def recall_block(self, query):
        return ""

    def finalize_turn(self, session, answer, *, extract_semantic):
        self.finalized.append((answer, extract_semantic))
        return {"episode_id": "episode", "semantic_memories_written": 0}


def test_agent_emits_root_lifecycle_and_compaction_events(tmp_path):
    recorder = TraceRecorder(tmp_path / "trace.jsonl")
    lifecycle = LifecycleManager("session", recorder)
    session = Session.create("goal", tmp_path)
    session.active_deferred_tools = ["specialized_tool"]
    specialized = Tool(
        name="specialized_tool",
        description="Specialized test capability",
        parameters={"type": "object", "properties": {}},
        call=lambda args, runtime: ToolResult.success(),
        defer_to_model=True,
    )
    llm = ScriptLLM([_final("done")])
    llm.context_limit = 5_000
    agent = Agent(
        llm,
        [specialized],
        session,
        SilentRenderer(),
        lifecycle=lifecycle,
        context_watermark=0.5,
        keep_recent_tool_results=0,
    )
    session.append_message({
        "role": "tool", "tool_call_id": "old",
        "content": json.dumps({"ok": True, "data": "x" * 40_000}),
    })

    assert agent.run("do it") == "done"
    assert session.active_deferred_tools == []

    events = [row["event"] for row in recorder.read()]
    assert events == [
        "user_prompt_submit",
        "agent_start",
        "pre_compact",
        "post_compact",
        "llm_start",
        "llm_end",
        "agent_stop",
    ]


def test_agent_stop_hook_can_reject_candidate_and_continue(tmp_path):
    lifecycle = LifecycleManager("session")
    calls = 0

    def stop_hook(event):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"decision": "deny", "reason": "answer is incomplete"}
        return None

    lifecycle.register(HookRegistration(
        event="agent_stop", name="completion-gate", callback=stop_hook
    ))
    agent = Agent(
        ScriptLLM([_final("first"), _final("second")]),
        [],
        Session.create("goal", tmp_path),
        SilentRenderer(),
        lifecycle=lifecycle,
    )

    assert agent.run("do it") == "second"
    assert calls == 2


def test_llm_failure_is_traced_without_destroying_resumable_state(tmp_path):
    recorder = TraceRecorder(tmp_path / "trace.jsonl")
    lifecycle = LifecycleManager("session", recorder)
    session = Session.create("goal", tmp_path)
    agent = Agent(
        BrokenLLM(), [], session, SilentRenderer(), lifecycle=lifecycle
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        agent.run("do it")
    assert session.current_run_status() == "running"
    assert [row["event"] for row in recorder.read()] == [
        "user_prompt_submit",
        "agent_start",
        "llm_start",
        "llm_error",
    ]


def test_subagent_uses_shared_lifecycle_with_agent_identity(tmp_path):
    recorder = TraceRecorder(tmp_path / "trace.jsonl")
    lifecycle = LifecycleManager("root-session", recorder)
    root = Session.create("root", tmp_path)
    root.begin_user_turn("root")
    spawn = make_spawn_agent_tool(
        ScriptLLM([_final("child done")]),
        [],
        max_depth=1,
        render_subagents=False,
    )

    result = spawn.call(
        {"task": "child"},
        tool_runtime_for_session(
            root,
            tool_name="spawn_agent",
            tool_call_id="call_1",
            workspace_dir=tmp_path,
            cwd_provider=root.get_cwd,
            lifecycle=lifecycle,
        ),
    )

    assert result.ok
    rows = recorder.read()
    lifecycle_rows = [
        row for row in rows
        if row["event"] in {"subagent_start", "subagent_stop"}
    ]
    assert [row["event"] for row in lifecycle_rows] == [
        "subagent_start", "subagent_stop"
    ]
    assert lifecycle_rows[0]["agent_task_id"] == result.data["task_id"]


def test_runtime_notification_preserves_user_turn_and_defers_episode(tmp_path):
    recorder = TraceRecorder(tmp_path / "trace.jsonl")
    lifecycle = LifecycleManager("session", recorder)
    session = Session.create("placeholder", tmp_path)
    memory = RecordingMemory()
    agent = Agent(
        ScriptLLM([_final("initial answer"), _final("background incorporated")]),
        [],
        session,
        SilentRenderer(),
        lifecycle=lifecycle,
        memory=memory,
    )
    expected_turn_id = f"{session.session_id}:1"
    task = session.control_plane.begin_task(
        root_turn_id=expected_turn_id,
        parent_id=None,
        tool_call_id="call_1",
        depth=1,
        task="background",
        requested_steps=5,
    )
    session.plan_manager.create_plan("goal", ["wait for background"])

    assert agent.run("real user goal") == "initial answer"
    original_run = session.active_run()
    assert original_run is not None and original_run.status == "completed"
    original_boundary = session.active_turn_start_message_index
    original_plan = session.plan_manager.snapshot()
    assert memory.finalized == []

    session.control_plane.finish_task(
        task.id,
        status="completed",
        steps_used=1,
        result="child result",
    )
    assert agent.run_runtime_event({
        "type": "task_notification",
        "task": {
            "id": task.id,
            "root_turn_id": expected_turn_id,
            "status": "completed",
            "result": "child result",
        },
    }) == "background incorporated"

    assert session.current_goal() == "real user goal"
    continuation = session.active_run()
    assert continuation is not None
    assert continuation.run_id != original_run.run_id
    assert continuation.parent_run_id == original_run.run_id
    assert continuation.root_run_id == original_run.root_run_id
    assert original_run.status == "completed"
    assert session.agent_root_turn_id == expected_turn_id
    assert session.active_turn_start_message_index == original_boundary
    assert session.plan_manager.snapshot() == original_plan
    assert memory.finalized == [("background incorporated", True)]
    events = [row["event"] for row in recorder.read()]
    assert events.count("user_prompt_submit") == 1
    assert events.count("runtime_event") == 1
