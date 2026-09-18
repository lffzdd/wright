from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from wright.agent import Agent
from wright.context import ContextBuilder, ContextCompactor
from wright.renderer import SilentRenderer
from wright.session import Session
from wright.tools.base import ToolCall, ToolResult


def _tool_result(call_id: str, data: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({"ok": True, "err": "", "data": data}),
    }


def test_context_projection_folds_without_rewriting_complete_history(tmp_path: Path):
    session = Session.create("context", tmp_path)
    session.append_message({"role": "assistant", "content": "calling"})
    session.append_message(_tool_result("call_a", "x" * 8_000))
    session.append_message({"role": "assistant", "content": "calling again"})
    session.append_message(_tool_result("call_b", "y" * 8_000))
    original = deepcopy([record.message for record in session.message_records])
    builder = ContextBuilder(
        ContextCompactor(SilentRenderer(), context_watermark=0.1, keep_recent_tool_results=1)
    )

    view = builder.build(
        session.message_records,
        tools=[],
        context_limit=1_000,
        output_reserve_tokens=0,
    )

    assert view.folded_record_ids == (session.message_records[1].id,)
    assert json.loads(view.messages[1]["content"])["folded"] is True
    assert [record.message for record in session.message_records] == original
    assert json.loads(session.message_records[1].message["content"])["data"] == "x" * 8_000


def test_context_view_has_no_mutable_alias_and_rebuild_is_stable(tmp_path: Path):
    session = Session.create("context", tmp_path)
    session.append_message({
        "role": "user",
        "content": "inspect",
        "parts": [{"type": "text", "text": "inspect"}],
        "provider_state": {"responses_output": [{"id": "opaque"}]},
    })
    builder = ContextBuilder(ContextCompactor(SilentRenderer()))

    first = builder.build(session.message_records, tools=[{"type": "function", "function": {"name": "x"}}])
    first.messages[0]["parts"][0]["text"] = "mutated"
    first.messages[0]["provider_state"]["responses_output"][0]["id"] = "changed"
    second = builder.build(session.message_records, tools=[{"type": "function", "function": {"name": "x"}}])

    assert session.message_records[0].message["parts"][0]["text"] == "inspect"
    assert session.message_records[0].message["provider_state"]["responses_output"][0]["id"] == "opaque"
    assert second.messages[0]["parts"][0]["text"] == "inspect"
    assert second.messages[0]["provider_state"]["responses_output"][0]["id"] == "opaque"


def test_session_records_own_message_data_not_caller_or_reader_aliases(tmp_path: Path):
    session = Session.create("context", tmp_path)
    message = {"role": "user", "content": "original", "parts": [{"type": "text", "text": "original"}]}
    session.append_message(message)
    message["parts"][0]["text"] = "caller mutation"
    projected = session.conversation_messages()
    projected[0]["parts"][0]["text"] = "reader mutation"

    assert session.message_records[0].message["parts"][0]["text"] == "original"


def test_context_keeps_tool_call_result_order_when_folding(tmp_path: Path):
    session = Session.create("context", tmp_path)
    session.begin_user_turn("task")
    turn = session.record_assistant_turn(
        "",
        {"tool_calls": []},
        "tool_calls",
        [ToolCall("read_file", {"path": "a"}, "call_a")],
    )
    session.record_tool_execution("call_a", ToolResult.success("x" * 8_000))
    for result in session.tool_executions.values():
        session.append_message(_tool_result(result.call.id, "x" * 8_000))
    builder = ContextBuilder(
        ContextCompactor(SilentRenderer(), context_watermark=0.1, keep_recent_tool_results=0)
    )

    view = builder.build(session.message_records, tools=[], context_limit=1_000, output_reserve_tokens=0)

    calls = [message for message in view.messages if message.get("tool_calls")]
    results = [message for message in view.messages if message.get("role") == "tool"]
    assert calls[0]["tool_calls"][0]["id"] == "call_a"
    assert [message["tool_call_id"] for message in results] == ["call_a"]
    assert turn.message_id == session.message_records[0].id


def test_agent_fails_explicitly_when_safe_projection_cannot_fit(tmp_path: Path):
    class NeverCalled:
        context_limit = 100

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _request, **_kwargs):
            self.calls += 1
            raise AssertionError("provider must not receive an over-budget request")
            yield

    llm = NeverCalled()
    session = Session.create("context", tmp_path)
    agent = Agent(llm, [], session, SilentRenderer())

    assert agent.run("too small") is None
    assert session.current_run_status() == "failed"
    assert llm.calls == 0
