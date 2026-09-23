import threading
import time

import pytest

from ..interaction import InteractionBroker
from ..tools.base import ToolCall, ToolResult
from ..ui_events import (
    EventPublisher,
    EventScope,
    RendererEventSubscriber,
    SessionEvents,
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
    renderer = SessionEvents(publisher)
    first = ToolCall("read", {"file": "a"}, "call-a")
    second = ToolCall("search", {"query": "x"}, "call-b")

    renderer.on_tool_call(first)
    renderer.on_tool_call(second)
    renderer.on_tool_phase(first, "awaiting_approval")
    renderer.on_tool_phase(first, "running")
    renderer.on_tool_output("call-a", "one")
    renderer.on_tool_result(second, ToolResult.success({"count": 2}))

    events = publisher.retained_events()
    assert [event.payload["call_id"] for event in events] == [
        "call-a", "call-b", "call-a", "call-a", "call-a", "call-b"
    ]
    assert [event.type for event in events[:4]] == [
        "tool.planned", "tool.planned", "tool.awaiting_approval", "tool.running"
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

    assert broker.resolve(request_id, "allow_once") is True
    assert broker.resolve(request_id, "deny") is False
    thread.join(timeout=1)
    assert result == ["allow_once"]

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
    resolved = [event for event in publisher.retained_events() if event.type == "interaction.resolved"]
    assert resolved[-1].payload["cancelled"] is True


def test_interaction_broker_keeps_transport_request_id_distinct_from_tool_call_id():
    publisher = EventPublisher(project_id="project", session_id="session")
    broker = InteractionBroker(publisher)
    result: list[object] = []
    thread = threading.Thread(
        target=lambda: result.append(
            broker.request("permission", {"request_id": "tool-call-1", "tool_name": "write"})
        )
    )
    thread.start()
    deadline = time.monotonic() + 1
    while not broker.snapshot() and time.monotonic() < deadline:
        time.sleep(0.005)
    request_id = broker.snapshot()[0]["request_id"]

    assert request_id != "tool-call-1"
    requested = [
        event for event in publisher.retained_events()
        if event.type == "interaction.requested"
    ][-1]
    assert requested.payload["request_id"] == request_id
    assert broker.resolve(request_id, "allow_once") is True
    thread.join(timeout=1)
    assert result == ["allow_once"]


def test_child_events_share_the_bus_without_entering_the_root_transcript():
    class Sink:
        def __init__(self) -> None:
            self.deltas: list[str] = []
            self.notices: list[str] = []
            self.agent_events: list[dict] = []

        def on_content_delta(self, piece: str) -> None:
            self.deltas.append(piece)

        def on_system_notice(self, text: str) -> None:
            self.notices.append(text)

        def on_agent_event(self, event: dict) -> None:
            self.agent_events.append(event)

    publisher = EventPublisher(project_id="project", session_id="session")
    sink = Sink()
    publisher.add_listener(RendererEventSubscriber(sink))
    root = SessionEvents(publisher)
    child = SessionEvents(
        publisher, scope=EventScope(depth=1, task_id="task-1"),
    )

    root.on_content_delta("root")
    child.on_content_delta("child draft")
    child.on_tool_call(ToolCall("read", {"file": "a"}, "call-1"))
    child.on_final("done")

    assert sink.deltas == ["root"]
    assert any("子Agent(d1)" in notice and "read" in notice for notice in sink.notices)
    assert any("收口: done" in notice for notice in sink.notices)
    child_events = [
        event for event in publisher.retained_events()
        if event.payload.get("agent_task_id") == "task-1"
    ]
    assert {event.payload["agent_depth"] for event in child_events} == {1}
    assert all(event.payload.get("agent_depth", 0) == 0 for event in publisher.retained_events() if event.type == "content.delta" and event.payload.get("piece") == "root")
