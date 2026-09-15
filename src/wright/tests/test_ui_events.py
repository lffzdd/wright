import threading
import time

import pytest

from ..interaction import InteractionBroker
from ..renderer import SilentRenderer
from ..tools.base import ToolCall, ToolResult
from ..ui_events import (
    EventPublisher,
    PublishingRenderer,
    UiEventEnvelope,
)


def test_ui_event_json_round_trip_and_unknown_rejection():
    publisher = EventPublisher(project_id="project", session_id="session")
    original = publisher.publish("content.final", {"content": "done"})

    restored = UiEventEnvelope.from_dict(original.to_dict())

    assert restored == original
    unknown = original.to_dict()
    unknown["type"] = "future.event"
    with pytest.raises(ValueError, match="unknown"):
        UiEventEnvelope.from_dict(unknown)


def test_event_sequence_and_replay_boundary():
    publisher = EventPublisher(
        project_id="project", session_id="session", max_events=2_000
    )
    for index in range(2_005):
        publisher.publish("content.delta", {"piece": str(index)})

    replay = publisher.replay(publisher.stream_id, 5)
    assert replay is not None
    assert replay[0].seq == 6
    assert replay[-1].seq == 2_005
    assert publisher.replay(publisher.stream_id, 4) is None
    assert publisher.replay("old-stream", 2_005) is None


def test_tool_events_merge_by_stable_call_id():
    publisher = EventPublisher(project_id="project", session_id="session")
    renderer = PublishingRenderer(publisher, direct_renderer=SilentRenderer())
    first = ToolCall("read", {"file": "a"}, "call-a")
    second = ToolCall("search", {"query": "x"}, "call-b")

    renderer.on_tool_call(first)
    renderer.on_tool_call(second)
    renderer.on_tool_output("call-a", "one")
    renderer.on_tool_result(second, ToolResult.success({"count": 2}))

    events = publisher.retained_events()
    assert [event.payload["call_id"] for event in events] == [
        "call-a", "call-b", "call-a", "call-b"
    ]


def test_interaction_broker_accepts_only_first_answer_and_closes_fail_closed():
    publisher = EventPublisher(project_id="project", session_id="session")
    broker = InteractionBroker(publisher)
    result: list[object] = []
    thread = threading.Thread(
        target=lambda: result.append(broker.request("permission", {"tool_name": "write"}))
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not broker.snapshot() and time.monotonic() < deadline:
        time.sleep(0.005)
    request_id = broker.snapshot()[0]["request_id"]

    assert broker.resolve(request_id, "y") is True
    assert broker.resolve(request_id, "n") is False
    thread.join(timeout=1)
    assert result == ["y"]

    cancelled: list[object] = []
    thread = threading.Thread(
        target=lambda: cancelled.append(broker.request("ask_user", {"question": "?"}))
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not broker.snapshot() and time.monotonic() < deadline:
        time.sleep(0.005)
    broker.close()
    thread.join(timeout=1)
    assert cancelled == [None]
