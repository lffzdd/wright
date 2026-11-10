from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path

from wright.application.agent import ContextBuilder, ContextCompactor
from wright.application.composition.agent_factory import create_agent
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall, ToolResult
from wright.utils import estimate_message_tokens


def test_projected_system_prompt_is_budgeted_without_rewriting_history(tmp_path):
    session = Session.create("context", tmp_path)
    session.append_message({
        "role": "system", "content": "static instructions",
        "parts": [{"type": "text", "text": "static instructions"}],
    })
    original = deepcopy(session.message_records[0].message)
    builder = ContextBuilder(ContextCompactor())
    projected_prompt = "current pinned memory " * 1000

    view = builder.build(
        session.message_records, tools=[], system_prompt=projected_prompt,
        context_limit=100, output_reserve_tokens=0,
    )

    assert view.messages[0]["content"] == projected_prompt
    assert view.messages[0]["parts"] == [{"type": "text", "text": projected_prompt}]
    assert view.history_tokens == estimate_message_tokens(view.messages[0])
    assert view.over_budget
    assert session.message_records[0].message == original


def _tool_result(call_id: str, data: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({"ok": True, "err": "", "data": data}),
    }


def test_compact_notice_resets_when_conversation_key_changes(tmp_path: Path):
    session_a = Session.create("context-a", tmp_path)
    session_b = Session.create("context-b", tmp_path)
    for session in (session_a, session_b):
        for name in ("call_a", "call_b", "call_c"):
            session.append_message({"role": "assistant", "content": "calling"})
            session.append_message(_tool_result(name, "x" * 8_000))
    notices = []
    compactor = ContextCompactor(
        context_watermark=0.1,
        keep_recent_tool_results=1,
        on_compact=lambda *args: notices.append(args),
    )
    builder = ContextBuilder(compactor)
    builder.build(
        session_a.message_records,
        tools=[],
        context_limit=1_000,
        output_reserve_tokens=0,
        conversation_key=session_a.session_id,
    )
    builder.build(
        session_b.message_records,
        tools=[],
        context_limit=1_000,
        output_reserve_tokens=0,
        conversation_key=session_b.session_id,
    )
    assert len(notices) == 2


def test_compact_notice_fires_once_per_watermark_crossing(tmp_path: Path):
    session = Session.create("context", tmp_path)
    for name in ("call_a", "call_b", "call_c"):
        session.append_message({"role": "assistant", "content": "calling"})
        session.append_message(_tool_result(name, "x" * 8_000))
    notices = []
    builder = ContextBuilder(ContextCompactor(
        context_watermark=0.1, keep_recent_tool_results=1,
        on_compact=lambda *args: notices.append(args),
    ))
    for _ in range(3):
        builder.build(session.message_records, tools=[], context_limit=1_000, output_reserve_tokens=0)
    assert len(notices) == 1
    builder.build(session.message_records, tools=[], context_limit=1_000_000, output_reserve_tokens=0)
    builder.build(session.message_records, tools=[], context_limit=1_000, output_reserve_tokens=0)
    assert len(notices) == 2


def test_compact_notice_is_once_under_concurrent_projection(tmp_path: Path):
    session = Session.create("context", tmp_path)
    for name in ("call_a", "call_b", "call_c"):
        session.append_message({"role": "assistant", "content": "calling"})
        session.append_message(_tool_result(name, "x" * 8_000))
    notices: list[tuple] = []
    notice_lock = threading.Lock()

    def record_compact(*args: object) -> None:
        with notice_lock:
            notices.append(args)

    compactor = ContextCompactor(
        context_watermark=0.1,
        keep_recent_tool_results=1,
        on_compact=record_compact,
    )
    builder = ContextBuilder(compactor)
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        builder.build(session.message_records, tools=[], context_limit=1_000, output_reserve_tokens=0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(notices) == 1


def test_compact_callback_cannot_overlap_a_reset_of_the_announce_flag(tmp_path: Path):
    """A concurrent under-watermark project must wait until on_compact returns.

    If `_compact_announced` were cleared between the check-and-set and the
    callback, a second crossing could fire on_compact while the first callback
    is still running.
    """
    session = Session.create("context", tmp_path)
    for name in ("call_a", "call_b", "call_c"):
        session.append_message({"role": "assistant", "content": "calling"})
        session.append_message(_tool_result(name, "x" * 8_000))
    started = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def record_compact(*_args: object) -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        started.set()
        assert release.wait(timeout=2)
        with state_lock:
            active -= 1

    builder = ContextBuilder(ContextCompactor(
        context_watermark=0.1, keep_recent_tool_results=1, on_compact=record_compact,
    ))

    def over_watermark() -> None:
        builder.build(
            session.message_records, tools=[], context_limit=1_000,
            output_reserve_tokens=0, conversation_key=session.session_id,
        )

    first = threading.Thread(target=over_watermark)
    first.start()
    assert started.wait(timeout=2)
    under = threading.Thread(target=lambda: builder.build(
        session.message_records, tools=[], context_limit=1_000_000,
        output_reserve_tokens=0, conversation_key=session.session_id,
    ))
    second = threading.Thread(target=over_watermark)
    under.start()
    second.start()
    release.set()
    for thread in (first, under, second):
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert max_active == 1


def test_context_projection_folds_without_rewriting_complete_history(tmp_path: Path):
    session = Session.create("context", tmp_path)
    session.append_message({"role": "assistant", "content": "calling"})
    session.append_message(_tool_result("call_a", "x" * 8_000))
    session.append_message({"role": "assistant", "content": "calling again"})
    session.append_message(_tool_result("call_b", "y" * 8_000))
    original = deepcopy([record.message for record in session.message_records])
    builder = ContextBuilder(
        ContextCompactor(context_watermark=0.1, keep_recent_tool_results=1)
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
    builder = ContextBuilder(ContextCompactor())

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
        [ToolCall("read", {"path": "a"}, "call_a")],
    )
    session.record_tool_execution("call_a", ToolResult.success("x" * 8_000))
    for result in session.tool_executions.values():
        session.append_message(_tool_result(result.call.id, "x" * 8_000))
    builder = ContextBuilder(
        ContextCompactor(context_watermark=0.1, keep_recent_tool_results=0)
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
    agent = create_agent(llm, [], session)

    assert agent.run("too small") is None
    assert session.current_run_status() == "failed"
    assert llm.calls == 0
