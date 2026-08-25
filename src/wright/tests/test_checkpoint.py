import json

import pytest

from ..agent import Agent
from ..checkpoint import CheckpointError, SessionCheckpointStore
from ..events import ContentDone
from ..renderer import SilentRenderer
from ..session import SessionState, UsageRecord
from ..tools.base import ToolCall, ToolResult
from ..util import build_tool_results_message


def _populated_session(tmp_path):
    workspace = tmp_path / "workspace"
    cwd = workspace / "nested"
    cwd.mkdir(parents=True)
    session = SessionState.create("checkpoint goal", workspace, max_steps=9)
    session.begin_user_turn("checkpoint goal")
    session.append_message({"role": "system", "content": "system"})
    session.append_message({"role": "user", "content": "do it"})
    session.set_cwd(cwd)

    session.plan_manager.create_plan("ship", ["write", "test"])
    session.plan_manager.update_step("step_1", "completed", note="written")
    session.plan_manager.update_step("step_2", "completed", note="passed")

    call = ToolCall("write_file", {"file": "a.txt", "content": "ok"}, "call_1")
    tool_turn = session.record_assistant_turn(
        assistant_raw=json.dumps({
            "tool_calls": [{"name": call.name, "arguments": call.arguments}],
            "final_answer": None,
        }),
        parsed={"tool_calls": [{"name": call.name, "arguments": call.arguments}]},
        route="tool_calls",
        tool_calls=[call],
    )
    session.record_usage_for_turn(tool_turn, UsageRecord(10, 4, 14))
    result = ToolResult.success({"path": str(workspace / "a.txt")})
    session.record_tool_execution("call_1", result, started_at=1.5, ended_at=2.0)
    session.append_message(build_tool_results_message([(call, result)]))

    final_turn = session.record_assistant_turn(
        assistant_raw=json.dumps({"tool_calls": [], "final_answer": "done"}),
        parsed={"tool_calls": [], "final_answer": "done"},
        route="final",
    )
    session.record_verification(final_turn, True, [])
    session.mark_completed()
    return session


def test_checkpoint_round_trips_complete_session_state(tmp_path):
    original = _populated_session(tmp_path)
    store = SessionCheckpointStore(tmp_path / "checkpoints")

    path = store.save(original)
    restored = store.load(original.session_id)

    assert path.stat().st_mode & 0o777 == 0o600
    assert restored.session_id == original.session_id
    assert restored.status == "completed"
    assert restored.user_goal == "checkpoint goal"
    assert restored.get_cwd() == original.get_cwd()
    assert restored.wire_messages() == original.wire_messages()
    assert restored.step_count == original.step_count
    assert restored.active_turn_start_step == original.active_turn_start_step
    assert (
        restored.active_turn_start_message_index
        == original.active_turn_start_message_index
    )
    assert restored.total_usage == original.total_usage
    assert restored.plan_manager.snapshot() == original.plan_manager.snapshot()
    assert restored.plan_manager.create_plan is not None

    execution = restored.tool_executions["call_1"]
    assert execution.call.arguments["file"] == "a.txt"
    assert execution.result.ok is True
    assert execution.started_at == 1.5
    assert execution.ended_at == 2.0
    assert restored.turns[-1].verification.approved is True
    assert restored.assistant_raw(restored.turns[-1]).endswith('"done"}')
    assert not list(store.directory.glob("*.tmp"))


def test_restored_plan_continues_step_ids_without_collision(tmp_path):
    original = _populated_session(tmp_path)
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    store.save(original)
    restored = store.load(original.session_id)

    restored.plan_manager.create_plan("next", ["new"], replace=True)
    assert restored.plan_manager.snapshot()["steps"][0]["id"] == "step_1"


def test_checkpoint_does_not_restore_live_autonomy_handles(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionState.create("autonomous", workspace)
    store = SessionCheckpointStore(tmp_path / "checkpoints")

    store.save(session)
    restored = store.load(session.session_id)

    assert restored.durable_task_store is None
    assert restored.autonomy_scheduler is None
    assert restored.loop_registry is None


def test_unknown_checkpoint_version_is_rejected(tmp_path):
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    store.directory.mkdir()
    path = store.path_for("abc123")
    path.write_text(json.dumps({"version": 999, "session": {}}))

    with pytest.raises(CheckpointError, match="version"):
        store.load("abc123")


def test_checkpoint_refuses_missing_workspace(tmp_path):
    session = SessionState.create("goal", tmp_path / "missing")
    store = SessionCheckpointStore(tmp_path / "checkpoints")

    with pytest.raises(CheckpointError, match="workspace_dir 不存在"):
        store.save(session)


def test_latest_checkpoint_uses_most_recent_file(tmp_path):
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    first = _populated_session(tmp_path / "first")
    second = _populated_session(tmp_path / "second")
    store.save(first)
    store.save(second)

    assert store.load_latest().session_id == second.session_id


class CrashLLM:
    context_limit = 128_000

    def __call__(self, messages):
        raise RuntimeError("simulated process crash")
        yield


class FinalLLM:
    context_limit = 128_000

    def __init__(self):
        self.messages = None

    def __call__(self, messages):
        self.messages = list(messages)
        yield ContentDone(json.dumps({"tool_calls": [], "final_answer": "resumed"}))


def test_agent_continues_running_checkpoint_without_new_user_message(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    first_session = SessionState.create("placeholder", workspace)
    first_agent = Agent(
        CrashLLM(), [], first_session, SilentRenderer(), checkpoint_store=store
    )

    with pytest.raises(RuntimeError, match="simulated process crash"):
        first_agent.run("survive this", max_steps=3)

    restored = store.load(first_session.session_id)
    assert restored.status == "running"
    user_messages_before = [
        message for message in restored.messages if message.get("role") == "user"
    ]

    llm = FinalLLM()
    second_agent = Agent(
        llm, [], restored, SilentRenderer(), checkpoint_store=store
    )
    assert second_agent.continue_run() == "resumed"
    assert restored.status == "completed"
    assert [
        message for message in restored.messages if message.get("role") == "user"
    ] == user_messages_before
    assert len([m for m in restored.messages if m.get("role") == "system"]) == 1


def test_pending_tool_calls_recover_as_unknown_instead_of_replaying(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionState.create("goal", workspace)
    session.begin_user_turn("write once")
    session.append_message({"role": "user", "content": "write once"})
    call = ToolCall("write_file", {"file": "a.txt", "content": "x"}, "c1")
    session.record_assistant_turn(
        "pending tool call",
        {"tool_calls": []},
        "tool_calls",
        [call],
    )
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    store.save(session)

    restored = store.load(session.session_id)

    execution = restored.tool_executions["c1"]
    assert execution.status == "failed"
    assert execution.result.data["error"]["type"] == "tool_execution_interrupted"
    assert "outcome is unknown" in execution.result.err
    assert "tool_execution_interrupted" in restored.messages[-1]["content"]


def test_list_recent_sessions(tmp_path):
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    s1 = _populated_session(tmp_path)
    store.save(s1)

    recent = store.list_recent_sessions(limit=5)
    assert len(recent) == 1
    assert recent[0]["session_id"] == s1.session_id
    assert recent[0]["user_goal"] == "checkpoint goal"
    assert recent[0]["status"] == "completed"
