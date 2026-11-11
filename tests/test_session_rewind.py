"""Cutting a conversation back to a turn, and forking it into a new session."""

from pathlib import Path

import pytest

from wright.application.session_runtime.publisher import EventPublisher
from wright.application.workspace.timeline import project_timeline
from wright.domain.model.session import HistoryRewriteError, Session
from wright.domain.model.tool import ToolCall, ToolResult
from wright.domain.policy.permission.settings import PermissionRule


def _turn(session: Session, prompt: str, answer: str, *, tool: str | None = None, plan: str | None = None):
    session.begin_user_turn(prompt)
    user_id = session.append_user_message(prompt)
    if tool is not None:
        session.record_assistant_turn("", {"tool_calls": []}, "tool_calls", [ToolCall("read", {"path": "a"}, tool)])
        session.record_tool_execution(tool, ToolResult.success("ok"))
    if plan is not None:
        session.plan_manager.apply_steps([{"title": plan, "status": "pending"}], objective=plan)
    session.append_message({"role": "assistant", "content": answer}, source="model")
    session.mark_completed()
    return user_id


def _three_turns(tmp_path: Path) -> tuple[Session, list[str]]:
    session = Session.create("goal", tmp_path, session_id="source")
    ids = [
        _turn(session, "first", "one", tool="call-1", plan="plan one"),
        _turn(session, "second", "two", tool="call-2", plan="plan two"),
        _turn(session, "third", "three", tool="call-3"),
    ]
    return session, ids


def _texts(session: Session) -> list[str]:
    return [item["text"] for item in project_timeline(session) if item["kind"] == "text"]


def test_truncate_drops_the_turn_and_everything_after_it(tmp_path):
    session, ids = _three_turns(tmp_path)
    second_run = list(session.runs.values())[1]
    counter = session.message_id_counter

    session.truncate_history(session.user_message_index(ids[1]))

    assert _texts(session) == ["first", "one"]
    assert set(session.tool_executions) == {"call-1"}
    assert all(turn.run_id != second_run.run_id for turn in session.turns)
    assert [run.goal for run in session.runs.values()] == ["first"]
    assert session.active_run_id is None
    assert session.committed_turn_ids == [f"source:{session.user_message_index(ids[0])}"]
    assert session.agent_root_turn_id == ""
    assert session.plan_manager.objective == "plan one"
    # A dropped id is never reused, so live views cannot confuse old and new items.
    assert session.append_user_message("again") == f"msg_{counter + 1}"


def test_a_new_turn_after_truncating_is_not_mistaken_for_a_committed_one(tmp_path):
    session, ids = _three_turns(tmp_path)
    session.truncate_history(session.user_message_index(ids[1]))

    session.begin_user_turn("second, retried")
    assert not session.is_turn_committed()


def test_truncate_recomputes_the_history_estimate(tmp_path):
    session, ids = _three_turns(tmp_path)
    fresh = Session.create("goal", tmp_path)
    _turn(fresh, "first", "one", tool="call-1")

    session.truncate_history(session.user_message_index(ids[1]))

    assert session.context_tokens == fresh.context_tokens
    assert session.request_context_breakdown == {}


def test_truncating_to_the_first_turn_clears_runs_and_plan(tmp_path):
    session, ids = _three_turns(tmp_path)
    revision = session.plan_manager.revision

    session.truncate_history(session.user_message_index(ids[0]))

    assert session.message_records == [] and session.runs == {} and session.tool_executions == {}
    assert not session.plan_manager.has_plan
    assert session.plan_manager.revision > revision


def test_only_a_turn_boundary_can_be_cut(tmp_path):
    session, ids = _three_turns(tmp_path)
    with pytest.raises(HistoryRewriteError):
        session.user_message_index("msg_999")
    with pytest.raises(HistoryRewriteError):
        session.truncate_history(session.user_message_index(ids[1]) + 1)
    interjection = session.append_message({"role": "user", "content": "btw"}, source="user_interjection")
    with pytest.raises(HistoryRewriteError):
        session.user_message_index(interjection)


def test_a_running_turn_cannot_be_cut(tmp_path):
    session, ids = _three_turns(tmp_path)
    session.begin_user_turn("fourth")
    session.append_user_message("fourth")
    with pytest.raises(HistoryRewriteError, match="still running"):
        session.truncate_history(session.user_message_index(ids[2]))
    assert len(session.runs) == 4


def test_turn_end_index_keeps_the_whole_turn(tmp_path):
    session, ids = _three_turns(tmp_path)
    second = session.user_message_index(ids[1])
    assert session.turn_end_index(ids[0]) == second
    reply = next(record.id for record in session.message_records[:second] if record.message["role"] == "assistant")
    assert session.turn_end_index(reply) == second
    assert session.turn_end_index(ids[2]) == len(session.message_records)


def test_fork_gets_a_new_identity_without_the_source_grants(tmp_path):
    session, ids = _three_turns(tmp_path)
    session.authorization.add_rule(PermissionRule("shell", id="allow-shell"))
    session.authorization.set_modes(permission_mode="acceptEdits", interaction_mode="agent")

    fork = session.detach_as_fork("forked", end=session.turn_end_index(ids[0]))

    assert fork.session_id == "forked"
    assert fork.authorization.session_id == "forked"
    assert fork.authorization.rules == ()
    assert fork.authorization.permission_mode == "acceptEdits"
    assert fork.session_label == "goal (fork)"
    assert _texts(fork) == ["first", "one"]
    assert {run.session_id for run in fork.runs.values()} == {"forked"}
    assert all(item.startswith("forked:") for item in fork.committed_turn_ids)
    assert fork.commands == {} and fork.pending_episode_finalizes == []
    with pytest.raises(HistoryRewriteError):
        fork.detach_as_fork("forked", end=0)


def test_rewriting_history_makes_every_viewer_reload(tmp_path):
    publisher = EventPublisher(project_id="p", session_id="s")
    first, inbox = publisher.subscribe()
    second, _ = publisher.subscribe()
    publisher.publish("turn.started", {"prompt": "hi", "message_index": 0}, turn_id="s:0")
    assert publisher.display.root is not None

    publisher.history_rewritten()

    assert publisher.display.root is None
    assert inbox.empty()
    assert publisher.take_stale(first) and publisher.take_stale(second)
