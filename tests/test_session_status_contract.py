"""All status producers use one patch dialect, locked to the Web reader."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wright.application.session.dispatch import _publish_execution
from wright.application.session.events import (
    EventScope,
    SessionEvents,
    UiEventEnvelope,
    model_turn_started,
    session_status,
)
from wright.application.session.publisher import EventPublisher
from wright.interfaces.rendering.subscriber import RendererEventSubscriber

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "web/src/session-status-contract.json").read_text()
)
CASES = [
    {**case, "valid": valid}
    for key, valid in (("valid", True), ("invalid", False))
    for case in CONTRACT[key]
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_status_contract_at_publish_and_decode(case):
    publisher = EventPublisher(project_id="project", session_id="session")
    envelope = publisher.publish("content.delta", {}).to_dict()
    envelope.update(type="session.status_changed", payload=case["payload"])
    if case["valid"]:
        event = publisher.publish("session.status_changed", case["payload"])
        assert event.payload == case["payload"]
        assert UiEventEnvelope.from_dict(envelope).payload == case["payload"]
    else:
        with pytest.raises(ValueError, match=r"session\.status_changed"):
            publisher.publish("session.status_changed", case["payload"])
        assert publisher.latest_seq == 1
        with pytest.raises(ValueError, match=r"session\.status_changed"):
            UiEventEnvelope.from_dict(envelope)


def test_model_turn_started_uses_turn_signal_not_legacy_status():
    assert model_turn_started({"turn_signal": "model_turn_started"})
    assert not model_turn_started({"status": "model_turn_started"})
    assert not model_turn_started({})


def test_agent_and_execution_producers_share_status_dialect_and_terminal_signal():
    publisher = EventPublisher(project_id="project", session_id="session")
    signals = []
    publisher.add_listener(RendererEventSubscriber(SimpleNamespace(
        on_turn_begin=lambda: signals.append("begin"),
        on_reasoning_delta=lambda _piece: None,
    )))
    runtime = SimpleNamespace(
        publisher=publisher, session_state=SimpleNamespace(session_id="session"),
    )
    _publish_execution(runtime, "queued", "directory busy")
    assert publisher.retained_events()[-1].payload == session_status(
        "session", lifecycle="open", execution="queued", queue_reason="directory busy",
    )
    assert signals == []
    publisher.publish("turn.started", {"prompt": "go"}, turn_id="turn")
    publisher.publish("reasoning.delta", {"piece": "first"})
    SessionEvents(publisher, scope=EventScope(depth=1, task_id="child")).on_turn_begin()
    assert signals == []
    assert publisher.display.view()["active_turn"]["segment"] == 0
    SessionEvents(publisher).on_turn_begin()
    assert publisher.retained_events()[-1].payload == session_status(
        "session", turn_signal="model_turn_started",
    )
    assert publisher.display.view()["active_turn"]["segment"] == 1
    assert signals == ["begin"]


def test_removed_snapshot_surface_is_rejected():
    publisher = EventPublisher(project_id="project", session_id="session")
    with pytest.raises(ValueError, match="unknown UI event type"):
        publisher.publish("session.snapshot", {})
    envelope = publisher.publish("content.delta", {}).to_dict()
    envelope["type"] = "session.snapshot"
    with pytest.raises(ValueError, match="unknown UI event type"):
        UiEventEnvelope.from_dict(envelope)
