"""Tests for base abstractions, common primitives, DTOs, event bus, and utilities."""

import pytest
from wright.base import BaseEntity, ValueObject
from wright.common import (
    CHARS_PER_TOKEN,
    DEFAULT_MODEL,
    DomainError,
    EntityNotFoundError,
    WrightError,
)
from wright.application.dto import RunAgentRequest, StreamEventDTO
from wright.core.event_bus import EventBus
from wright.utils.string_diff import compute_unified_diff, render_colored_diff
from wright.utils.token_counter import estimate_tokens


from dataclasses import dataclass


def test_base_entity_and_value_object():
    @dataclass(eq=False)
    class UserEntity(BaseEntity):
        name: str = ""

    @dataclass(frozen=True)
    class PositionVO(ValueObject):
        x: int
        y: int

    u1 = UserEntity(id="u1", name="Alice")
    u2 = UserEntity(id="u1", name="Alice Clone")
    u3 = UserEntity(id="u3", name="Bob")

    assert u1 == u2
    assert u1 != u3
    assert hash(u1) == hash(u2)

    p1 = PositionVO(x=10, y=20)
    p2 = PositionVO(x=10, y=20)
    assert p1 == p2
    with pytest.raises(Exception):
        p1.x = 30  # Immutable


def test_common_constants_and_errors():
    assert CHARS_PER_TOKEN == 4
    assert DEFAULT_MODEL == "deepseek-chat"

    err = EntityNotFoundError("item not found")
    assert isinstance(err, DomainError)
    assert isinstance(err, WrightError)


def test_application_dtos():
    req = RunAgentRequest(prompt="fix bugs", model="gpt-4")
    assert req.prompt == "fix bugs"
    assert req.model == "gpt-4"

    event = StreamEventDTO(
        event_type="text_delta",
        turn_id="turn-1",
        content="hello",
        payload={"chunk": 1},
    )
    d = event.to_dict()
    assert d["event_type"] == "text_delta"
    assert d["content"] == "hello"
    assert d["payload"]["chunk"] == 1


def test_event_bus():
    bus = EventBus()
    received = []

    unsub = bus.subscribe("agent.started", lambda data: received.append(data))
    bus.publish("agent.started", {"id": "123"})
    assert received == [{"id": "123"}]

    unsub()
    bus.publish("agent.started", {"id": "456"})
    assert len(received) == 1


def test_diff_and_token_counter():
    diff = compute_unified_diff("hello\n", "world\n", fromfile="a.txt", tofile="b.txt")
    assert "--- a.txt" in diff
    assert "+++ b.txt" in diff

    colored = render_colored_diff(diff)
    assert "\033[" in colored

    assert estimate_tokens("12345678") == 2
