"""Failed and cancelled turns stay visible where they happened."""

import pytest

from wright.application.session.history_projection import project_history
from wright.application.workspace.timeline import project_timeline
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.interfaces.web.runtime_manager import _tool_state


def _turn(session, prompt, answer=None):
    session.begin_user_turn(prompt)
    session.append_user_message(prompt)
    if answer is not None:
        session.append_message({"role": "assistant", "content": answer}, source="model")


def test_outcomes_are_placed_after_their_own_turn(tmp_path):
    session = Session.create("goal", tmp_path)
    _turn(session, "first")
    session.mark_failed(error="Model request failed (HTTP 400): bad")
    _turn(session, "second")
    session.mark_cancelled()
    _turn(session, "third", answer="done")
    session.mark_completed()

    kinds = [(item["kind"], item.get("role") or item.get("status"), item.get("text", "")) for item in project_timeline(session)]
    assert kinds == [
        ("text", "user", "first"),
        ("status", "failed", "Model request failed (HTTP 400): bad"),
        ("text", "user", "second"),
        ("status", "cancelled", ""),
        ("text", "user", "third"),
        ("text", "assistant", "done"),
    ]


@pytest.mark.parametrize(("status", "phase"), [
    ("pending", "planned"),
    ("planned", "planned"),
    ("awaiting_approval", "awaiting_approval"),
    ("running", "running"),
    ("succeeded", "succeeded"),
    ("failed", "failed"),
    ("timeout", "failed"),
    ("unknown", "unknown"),
    ("", "failed"),
    (None, "failed"),
])
def test_tool_phases_match_across_timeline_active_turn_and_history(tmp_path, status, phase):
    session = Session.create("goal", tmp_path)
    _turn(session, "read")
    call = ToolCall("read_file", {"path": "note.md"}, "call-1")
    session.record_assistant_turn("", {}, "tool_calls", [call])
    session.tool_executions[call.id].status = status
    run = session.active_run()
    session.mark_completed()

    tool = next(item for item in project_timeline(session) if item.get("call_id") == call.id)
    assert tool["phase"] == phase
    assert project_history(session, set(), {run.run_id})[0]["tools"][0]["phase"] == phase
    # An absent status on an active run still represents a planned invocation.
    active_phase = phase if status else "planned"
    assert _tool_state({"call_id": call.id, "status": status})["phase"] == active_phase
    assert _tool_state({"call_id": call.id, "phase": status})["phase"] == active_phase


def test_live_turn_without_an_index_keeps_persisted_history(tmp_path):
    session = Session.create("goal", tmp_path)
    _turn(session, "first", answer="kept")
    persisted = [item.get("text") for item in project_timeline(session)]
    for live in (
        None,
        {"timeline": [{"id": "live", "kind": "text", "text": "streaming"}]},
        {"message_index": True, "timeline": [{"id": "live", "kind": "text", "text": "streaming"}]},
    ):
        assert [item.get("text") for item in project_timeline(session, live_turn=live)] == persisted


def test_live_turn_replaces_only_the_indexed_suffix(tmp_path):
    session = Session.create("goal", tmp_path)
    _turn(session, "first", answer="kept")
    start = len(session.message_records)
    _turn(session, "second", answer="replaced")
    timeline = project_timeline(session, live_turn={
        "message_index": start,
        "timeline": [{"id": "live", "kind": "text", "role": "user", "text": "second", "order": start}],
    })
    assert [item.get("text") for item in timeline] == ["first", "kept", "second"]
