"""Native Anthropic Messages transport, against fake HTTP responses."""

from __future__ import annotations

import json

import httpx2
import pytest

from wright.domain.model.llm import (
    ContentDelta,
    ContentDone,
    ModelRequest,
    ReasoningDelta,
    UsageEvent,
)
from wright.domain.model.llm.anthropic_adapter import (
    MessagesAdapter,
    checks_thinking_binding,
    supports_adaptive_thinking,
)
from wright.domain.model.llm.model_errors import resolve_transport
from wright.domain.model.llm.provider_usage import usage_from_provider
from wright.domain.model.llm.wire import assistant_message
from wright.infrastructure.llm.anthropic_client import sdk_base_url
from wright.infrastructure.llm.llm import LLMClient

TOOLS = (
    {"name": "read", "description": "Read a file", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }},
    {"name": "call_tool", "description": "Call a deferred tool", "parameters": {
        "type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
    }},
)

USAGE = {
    "input_tokens": 12, "output_tokens": 30,
    "cache_read_input_tokens": 900, "cache_creation_input_tokens": 40,
}


def _sse(*events: dict) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def _stream_body(stop_reason: str = "tool_use") -> bytes:
    return _sse(
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {**USAGE, "output_tokens": 1},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Need the file."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-abc"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Reading "}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "it."}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": "}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "\"a.py\"}"}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": USAGE},
        {"type": "message_stop"},
    )


class FakeAnthropic:
    """Records each request and answers with the next canned response."""

    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def _stream_response(stop_reason: str = "tool_use") -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_stream_body(stop_reason))


def _json_response(payload: dict) -> httpx2.Response:
    return httpx2.Response(200, json=payload)


def _client(fake: FakeAnthropic, *, model: str = "claude-opus-5-5", stream: bool = True) -> LLMClient:
    client = LLMClient(
        base_url="https://api.anthropic.com/v1/", api_key="sk-ant-test", model=model,
        stream=stream, max_attempts=1,
    )
    client.anthropic_http_client = httpx2.Client(transport=httpx2.MockTransport(fake))
    return client


def _history() -> list[dict]:
    return [
        {"role": "system", "content": "You are Wright."},
        {"role": "user", "content": "Open a.py"},
    ]


def _request(messages: list[dict], durable: int | None = None) -> ModelRequest:
    return ModelRequest(messages=tuple(messages), tools=TOOLS, cache_prefix_messages=durable)


def test_auto_transport_picks_messages_for_the_anthropic_host():
    assert resolve_transport("https://api.anthropic.com/v1/", "auto") == "messages"
    assert resolve_transport("https://api.openai.com/v1", "auto") == "responses"
    assert resolve_transport("https://example.test/v1", "messages") == "messages"
    assert sdk_base_url("https://api.anthropic.com/v1/") == "https://api.anthropic.com"


def test_stream_yields_deltas_then_a_done_with_replayable_blocks():
    fake = FakeAnthropic(_stream_response())
    events = list(_client(fake)(_request(_history())))

    assert [e.piece for e in events if isinstance(e, ReasoningDelta)] == ["Need the file."]
    assert "".join(e.piece for e in events if isinstance(e, ContentDelta)) == "Reading it."
    done = events[-1]
    assert isinstance(done, ContentDone)
    assert done.finish_reason == "tool_calls"
    assert done.content == "Reading it."
    assert done.tool_calls == [{
        "id": "toolu_1", "type": "function",
        "function": {"name": "read", "arguments": json.dumps({"path": "a.py"})},
    }]
    blocks = done.provider_state["anthropic_content"]
    assert blocks[0] == {"type": "thinking", "thinking": "Need the file.", "signature": "sig-abc"}
    assert [block["type"] for block in blocks] == ["thinking", "text", "tool_use"]

    usage = next(e for e in events if isinstance(e, UsageEvent))
    record = usage_from_provider(usage.usage)
    assert record.cache_read_tokens == 900
    assert record.prompt_tokens == 12 + 900 + 40


def test_request_has_fixed_tools_cache_breakpoints_and_thinking():
    fake = FakeAnthropic(_stream_response("end_turn"))
    reminder = {"role": "user", "content": "<plan-state>step 1</plan-state>"}
    list(_client(fake)(_request([*_history(), reminder], durable=2)))

    request = fake.requests[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["authorization"] == "Bearer sk-ant-test"
    assert "thinking-binding-controls-2026-08-01" in request.headers["anthropic-beta"]
    body = fake.body()
    assert [tool["name"] for tool in body["tools"]] == ["read", "call_tool"]
    assert all("strict" not in tool and "cache_control" not in tool for tool in body["tools"])
    assert body["system"] == [
        {"type": "text", "text": "You are Wright.", "cache_control": {"type": "ephemeral"}},
    ]
    # History and the transient reminder share one user turn; only the history is marked.
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "Open a.py", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "<plan-state>step 1</plan-state>"},
    ]}]
    assert body["thinking"] == {
        "type": "adaptive", "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert body["stream"] is True


def test_second_request_replays_the_assistant_turn_unchanged():
    fake = FakeAnthropic(_stream_response(), _stream_response("end_turn"))
    client = _client(fake)
    history = _history()
    done = list(client(_request(history)))[-1]
    first_tools = fake.body(0)["tools"]

    history.append(assistant_message(done))
    history.append({"role": "tool", "tool_call_id": "toolu_1", "content": json.dumps({"ok": True, "data": "x = 1"})})
    list(client(_request(history)))

    body = fake.body(1)
    assert body["tools"] == first_tools
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == done.provider_state["anthropic_content"]
    assert body["messages"][2] == {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "toolu_1",
        "content": json.dumps({"ok": True, "data": "x = 1"}),
        "cache_control": {"type": "ephemeral"},
    }]}
    # The first request's user turn is the same apart from its moved breakpoint.
    assert body["messages"][0] == {"role": "user", "content": [{"type": "text", "text": "Open a.py"}]}


def test_breakpoint_skips_thinking_blocks():
    adapter = MessagesAdapter(lambda _id: "")
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "provider_state": {"anthropic_content": [
            {"type": "thinking", "thinking": "", "signature": "s"},
        ]}},
    ]
    _system, wire = adapter.encode_request(messages)
    assert "cache_control" not in wire[1]["content"][0]
    assert wire[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_assistant_turns_from_another_provider_are_rebuilt():
    adapter = MessagesAdapter(lambda _id: "")
    _system, wire = adapter.encode_request([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "ok", "reasoning_content": "hidden", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{\"path\": \"b\"}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
        {"role": "tool", "tool_call_id": "call_2", "content": "{}"},
    ], cache_prefix_messages=0)
    assert wire[1] == {"role": "assistant", "content": [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "b"}},
    ]}
    assert [block["tool_use_id"] for block in wire[2]["content"]] == ["call_1", "call_2"]
    assert all("cache_control" not in block for turn in wire for block in turn["content"])


def test_tool_images_join_the_tool_result_turn():
    adapter = MessagesAdapter(lambda _id: "", lambda _ref: "data:image/png;base64,AAAA")
    result = {"ok": True, "artifacts": [{"id": "img1", "call_id": "call_1", "media_type": "image/png"}]}
    _system, wire = adapter.encode_request([
        {"role": "user", "content": "shot"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "screenshot", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(result)},
    ])
    assert len(wire) == 3
    kinds = [block["type"] for block in wire[2]["content"]]
    assert kinds == ["tool_result", "text", "image"]
    assert wire[2]["content"][2]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}


def test_non_streaming_request_and_truncation_is_not_executable():
    fake = FakeAnthropic(_json_response({
        "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": "{\"a\":"}],
        "stop_reason": "max_tokens", "stop_sequence": None, "usage": USAGE,
    }))
    events = list(_client(fake, model="claude-haiku-4-5", stream=False)(
        _history(), response_format={"type": "json_object"},
    ))
    body = fake.body()
    assert "thinking" not in body and "output_config" not in body and "stream" not in body
    assert body["max_tokens"] == 16_000
    assert "anthropic-beta" not in fake.requests[0].headers
    assert events[-1].finish_reason == "length"


@pytest.mark.parametrize(("model", "adaptive", "binding"), [
    ("claude-opus-5-5", True, True),
    ("anthropic/claude-sonnet-5-5", True, True),
    ("claude-fable-5-1", True, True),
    ("claude-fable-5", True, False),
    ("claude-opus-4-6", True, False),
    ("claude-sonnet-4-5", False, False),
    ("claude-haiku-4-5-20251001", False, False),
    ("claude-opus-4-20250514", False, False),
])
def test_thinking_settings_by_model(model, adaptive, binding):
    assert supports_adaptive_thinking(model) is adaptive
    assert checks_thinking_binding(model) is binding
