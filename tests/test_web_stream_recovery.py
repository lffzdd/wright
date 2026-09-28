import threading
from types import SimpleNamespace

from wright.application.session.events import notice_text
from wright.application.session.history_projection import project_history
from wright.application.session.publisher import EventPublisher
from wright.interfaces.web.websocket import opening_frames


def test_snapshot_watermark_is_the_seq_captured_with_the_body():
    publisher = EventPublisher(project_id="project", session_id="session")
    handle = SimpleNamespace(publisher=publisher, snapshot=lambda: publisher.capture(
        lambda seq, stream_id: {"stream_id": stream_id, "last_seq": seq, "marker": "body"}
    ))
    _subscriber, inbox, frames, cursor = _open(handle)
    assert frames[0]["snapshot"]["last_seq"] == 0
    assert cursor == 0
    publisher.publish("content.delta", {"piece": "during-send"})
    delivered = inbox.get_nowait()
    assert delivered.seq == cursor + 1
    assert delivered.payload["piece"] == "during-send"


def _open(handle):
    from wright.interfaces.web.websocket import opening_frames as frames

    subscriber, inbox = handle.publisher.subscribe()
    opening, cursor = frames(handle, None, None)
    if opening and opening[0]["type"] != "snapshot_required":
        return subscriber, inbox, opening, cursor
    # No cursor asks for a snapshot when replay cannot continue. last_seq None
    # replays nothing, so force the snapshot path the way a new stream does.
    opening, cursor = opening_frames(handle, "missing-stream", 0)
    return subscriber, inbox, opening, cursor


def test_concurrent_publish_keeps_queue_order_equal_to_sequence():
    publisher = EventPublisher(project_id="project", session_id="session")
    _subscriber, inbox = publisher.subscribe()
    barrier = threading.Barrier(2)

    def publish(piece: str) -> None:
        barrier.wait(2)
        publisher.publish("content.delta", {"piece": piece})

    threads = [threading.Thread(target=publish, args=(piece,)) for piece in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    first = inbox.get_nowait()
    second = inbox.get_nowait()
    assert first.seq < second.seq
    assert [first.seq, second.seq] == [1, 2]


def test_full_subscriber_queue_is_marked_stale_instead_of_growing():
    publisher = EventPublisher(project_id="project", session_id="session", subscriber_queue_max=1)
    subscriber, inbox = publisher.subscribe()
    publisher.publish("content.delta", {"piece": "one"})
    publisher.publish("content.delta", {"piece": "two"})
    assert publisher.take_stale(subscriber) is True
    assert inbox.empty()
    assert publisher.take_stale(subscriber) is False


def test_evicted_ring_still_folds_the_current_view():
    publisher = EventPublisher(project_id="project", session_id="session", max_events=2)
    publisher.publish("turn.started", {"prompt": "go"}, turn_id="turn-1")
    publisher.publish("content.delta", {"piece": "alpha "})
    publisher.publish("content.delta", {"piece": "beta"})
    assert publisher.replay(publisher.stream_id, 0) is None
    assert publisher.display.view()["active_turn"]["content"] == "alpha beta"
    fresh = EventPublisher(project_id="project", session_id="session")
    for event in publisher.retained_events():
        fresh.display.apply(event)
    assert fresh.display.view()["active_turn"] is None


def test_fold_at_a_watermark_matches_replaying_events_up_to_that_watermark():
    publisher = EventPublisher(project_id="project", session_id="session")
    publisher.publish("turn.started", {"prompt": "go", "attachments": [{"id": "att-1"}]}, turn_id="turn-1")
    publisher.publish("content.delta", {"piece": "hel"})
    watermark = publisher.latest_seq
    captured = publisher.capture(lambda seq, _stream: (seq, publisher.display.view()))
    publisher.publish("content.delta", {"piece": "lo"})
    assert captured[0] == watermark
    replayed = EventPublisher(project_id="project", session_id="session")
    for event in publisher.retained_events():
        if event.seq <= watermark:
            replayed.display.apply(event)
    assert replayed.display.view() == captured[1]
    for event in publisher.retained_events():
        if event.seq > watermark:
            replayed.display.apply(event)
    assert replayed.display.view() == publisher.display.view()


def test_child_fold_does_not_replace_root_content_or_usage():
    publisher = EventPublisher(project_id="project", session_id="session")
    publisher.publish("turn.started", {"prompt": "parent"}, turn_id="root")
    publisher.publish("content.final", {"content": "root answer"})
    publisher.publish("usage.request", {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4})
    publisher.publish("content.final", {"content": "child", "agent_depth": 1, "agent_task_id": "task-1"})
    publisher.publish("usage.request", {"prompt_tokens": 9, "completion_tokens": 9, "total_tokens": 18, "agent_depth": 1, "agent_task_id": "task-1"})
    view = publisher.display.view()
    assert view["active_turn"]["content"] == "root answer"
    assert view["request_usage"]["total_tokens"] == 4
    assert view["agents"][0]["content"] == "child"
    assert view["agents"][0]["request_usage"]["total_tokens"] == 18


def test_history_keeps_distinct_turns_and_failed_runs():
    turn_a = SimpleNamespace(
        step=1, step_id="turn-a", run_id="run-a", route="final", error=None,
        message_id="msg-a", parsed={"final_answer": "first"},
    )
    turn_b = SimpleNamespace(
        step=2, step_id="turn-b", run_id="run-b", route="final", error=None,
        message_id="msg-b", parsed={"final_answer": ""},
    )
    failed = SimpleNamespace(
        run_id="run-c", status="failed", goal="继续", result="", error="provider down",
        tool_execution_ids=[],
    )
    session = SimpleNamespace(
        turns=[turn_a, turn_b],
        runs={"run-a": SimpleNamespace(run_id="run-a", tool_execution_ids=[]), "run-c": failed},
        tool_executions={},
        message_records=[
            SimpleNamespace(id="user-a", message={"role": "user", "content": "继续", "attachments": []}),
            SimpleNamespace(id="msg-a", message={"role": "assistant", "content": "first"}),
            SimpleNamespace(id="user-b", message={"role": "user", "content": "继续", "attachments": []}),
            SimpleNamespace(id="msg-b", message={"role": "assistant", "content": ""}),
        ],
        attachment_records=lambda _ids: [],
    )
    history = project_history(session, {"turn-a", "turn-b"}, {"run-c"})
    assert [item["turn_id"] for item in history] == ["turn-a", "turn-b", "run-c"]
    assert [item["user"] for item in history[:2]] == ["继续", "继续"]
    assert history[1]["assistant"] == ""
    assert history[2]["status"] == "failed"
    assert history[2]["assistant"] == "provider down"


def test_notice_text_matches_the_snapshot_shape():
    assert notice_text("command.rejected", {"reason": "closed"}) == "closed"
    assert "disk" in notice_text("system.checkpoint_error", {"error": "disk"})


def test_frontend_contract_matches_backend_event_types():
    import json
    from pathlib import Path

    from wright.application.session.events import UI_EVENT_TYPES, UI_EVENT_VERSION

    payload = json.loads((Path(__file__).resolve().parents[1] / "web/src/ui-event-contract.json").read_text())
    assert payload["version"] == UI_EVENT_VERSION == 2
    assert set(payload["types"]) == UI_EVENT_TYPES
