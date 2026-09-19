import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from ..agent import Agent
from ..attachments import AttachmentStore
from ..events import ContentDone, UsageEvent
from ..llm import LLMClient
from ..memory.llm_util import side_query
from ..model import ModelRequest
from ..protocol import TurnAbort, parse_turn
from ..renderer import SilentRenderer
from ..session import Session
from ..tools.base import Tool, ToolResult


def _client(handler, *, stream):
    llm = LLMClient(
        "https://test.invalid/v1",
        "test-key",
        "test-model",
        stream=stream,
        max_attempts=1,
    )
    llm.client.close()
    llm.client = OpenAI(
        base_url="https://test.invalid/v1",
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return llm


def _responses_client(handler, *, stream):
    llm = LLMClient(
        "https://api.openai.com/v1",
        "test-key",
        "test-model",
        stream=stream,
        max_attempts=1,
        transport="responses",
    )
    llm.client.close()
    llm.client = OpenAI(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return llm


def _reply(message, finish="stop"):
    return {
        "id": "completion",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", **message},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _sse(deltas, finish="tool_calls"):
    chunks = []
    for delta in deltas:
        chunks.append(
            {
                "id": "completion",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
        )
    if finish is not None:
        chunks.append(
            {
                "id": "completion",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        )
    data = (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )
    return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})


def _call(index, call_id=None, name=None, arguments=""):
    call = {"index": index, "function": {"arguments": arguments}}
    if call_id:
        call.update(id=call_id, type="function")
    if name:
        call["function"]["name"] = name
    return call


def test_stream_assembles_interleaved_calls_and_keeps_reasoning_and_usage():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _sse(
            [
                {"reasoning_content": "provider reasoning"},
                {
                    "content": "Checking both",
                    "tool_calls": [_call(1, "call_b", "read", '{"file":')],
                },
                {"tool_calls": [_call(0, "call_a", "read", '{"file":"a"}')]},
                {"tool_calls": [_call(1, arguments='"中文.txt"}')]},
            ]
        )

    llm = _client(handler, stream=True)
    schemas = [
        {
            "type": "function",
            "function": {"name": "read", "parameters": {"type": "object"}},
        }
    ]
    try:
        events = list(llm([{"role": "user", "content": "inspect"}], tools=schemas))
        turn = parse_turn(events[-1])
        assert [call.id for call in turn.tool_calls] == ["call_a", "call_b"]
        assert turn.tool_calls[1].arguments == {"file": "中文.txt"}
        assert turn.assistant_message["reasoning_content"] == "provider reasoning"
        assert turn.assistant_message["content"] == "Checking both"
        assert any(isinstance(event, UsageEvent) for event in events)
        assert requests[0]["tools"] == schemas
        assert "response_format" not in requests[0]
    finally:
        llm.client.close()


@pytest.mark.parametrize("finish", [None, "length", "content_filter"])
def test_stream_cannot_execute_a_call_without_successful_completion(finish):
    llm = _client(
        lambda request: _sse(
            [
                {"tool_calls": [_call(0, "call_a", "write", "{}")]},
            ],
            finish=finish,
        ),
        stream=True,
    )
    try:
        with pytest.raises(TurnAbort):
            parse_turn(list(llm([]))[-1])
    finally:
        llm.client.close()


@pytest.mark.parametrize("stream", [False, True])
def test_real_sdk_round_trip_sends_each_result_with_original_id(tmp_path, stream):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            calls = [
                _call(0, "api_a", "echo", '{"value":"甲"}'),
                _call(1, "api_b", "echo", '{"value":"乙"}'),
            ]
            if stream:
                return _sse([{"content": "Checking", "tool_calls": calls}])
            return httpx.Response(
                200,
                json=_reply(
                    {
                        "content": "Checking",
                        "tool_calls": [
                            {k: v for k, v in c.items() if k != "index"} for c in calls
                        ],
                    },
                    "tool_calls",
                ),
            )
        messages = body["messages"]
        assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
            "api_a",
            "api_b",
        ]
        assistant = next(m for m in messages if m["role"] == "assistant")
        assert assistant["content"] == "Checking"
        assert len(assistant["tool_calls"]) == 2
        assert [
            json.loads(m["content"])["data"] for m in messages if m["role"] == "tool"
        ] == ["甲", "乙"]
        assert "response_format" not in body
        return (
            _sse([{"content": "done"}], finish="stop")
            if stream
            else httpx.Response(200, json=_reply({"content": "done"}))
        )

    llm = _client(handler, stream=stream)
    tool = Tool(
        "echo",
        "Return value",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        lambda args, runtime: ToolResult.success(args["value"]),
    )
    session = Session.create("test", tmp_path)
    try:
        assert Agent(llm, [tool], session, SilentRenderer()).run("test") == "done"
        assert len(requests) == 2
        assert session.turns[0].parsed["content"] == "Checking"
    finally:
        llm.client.close()


def test_side_queries_use_json_without_mutating_shared_client():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json=_reply({"content": '{"selected_memories": []}'})
        )

    llm = _client(handler, stream=False)
    try:
        schemas = [
            {
                "type": "function",
                "function": {"name": "parent_tool", "parameters": {"type": "object"}},
            }
        ]
        list(llm([], tools=schemas))
        assert json.loads(side_query(llm, "Return JSON", "Select")) == {
            "selected_memories": []
        }
        list(llm([]))
        assert requests[0]["tools"] == schemas
        assert "tools" not in requests[1]
        assert requests[1]["response_format"] == {"type": "json_object"}
        assert "tools" not in requests[2] and "response_format" not in requests[2]
    finally:
        llm.client.close()


def test_model_request_uses_its_run_snapshot_not_mutable_client_model():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_reply({"content": "ok"}))

    llm = _client(handler, stream=False)
    llm.model = "new-session-default"
    try:
        list(llm(ModelRequest(
            messages=({"role": "user", "content": "continue"},),
            model="run-snapshot-model",
        )))
        assert requests[0]["model"] == "run-snapshot-model"
    finally:
        llm.client.close()


@pytest.mark.parametrize("transport", ["chat", "responses"])
def test_internal_tool_schema_is_encoded_by_the_selected_adapter(transport):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if transport == "responses":
            return httpx.Response(200, json={
                "id": "resp", "object": "response", "created_at": 0,
                "status": "completed", "model": "test",
                "output": [{
                    "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "ok"}],
                }],
            })
        return httpx.Response(200, json=_reply({"content": "ok"}))

    llm = (
        _responses_client(handler, stream=False)
        if transport == "responses" else _client(handler, stream=False)
    )
    try:
        request = ModelRequest(
            messages=({"role": "user", "content": "hello"},),
            tools=({
                "name": "read_file", "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },),
            transport=transport,
        )
        assert list(llm(request))[-1].content == "ok"
        body = requests[0]
        if transport == "chat":
            assert body["tools"] == [{
                "type": "function",
                "function": {
                    "name": "read_file", "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
        else:
            assert body["tools"] == [{
                "type": "function", "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
                "strict": False,
            }]
    finally:
        llm.client.close()


def test_responses_continuation_state_is_never_silently_sent_to_chat():
    llm = _client(lambda _request: pytest.fail("must not call provider"), stream=False)
    try:
        with pytest.raises(ValueError, match="continuation state"):
            list(llm(ModelRequest(messages=({
                "role": "assistant",
                "provider_state": {"responses_output": [{"type": "message"}]},
            },), transport="chat")))
    finally:
        llm.client.close()


def test_chat_adapter_sends_image_as_data_url(tmp_path):
    from io import BytesIO

    from PIL import Image

    image = BytesIO()
    Image.new("RGB", (4, 3), "red").save(image, format="PNG")
    store = AttachmentStore(tmp_path / "attachments", "session")
    record = store.register_bytes("red.png", image.getvalue(), {})
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_reply({"content": "seen"}))

    llm = _client(handler, stream=False)
    llm.attachment_store = store
    llm.session_attachments = {record.id: record}
    try:
        events = list(llm([{
            "role": "user", "content": "describe", "attachments": [record.id],
            "parts": [
                {"type": "text", "text": "describe"},
                {"type": "image", "attachment_id": record.id, "detail": "auto"},
            ],
        }]))
        assert events[-1].content == "seen"
        content = requests[0]["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "describe"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    finally:
        llm.client.close()


def test_responses_adapter_normalizes_input_tools_usage_and_provider_state():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "created_at": 0,
            "status": "completed", "model": "test-model",
            "output": [{
                "id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "done", "annotations": []}],
            }],
            "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
        })

    llm = _responses_client(handler, stream=False)
    try:
        events = list(llm([{"role": "user", "content": "hello", "parts": [{"type": "text", "text": "hello"}]}], tools=[{
            "type": "function", "function": {"name": "echo", "description": "echo", "parameters": {"type": "object"}},
        }]))
        assert events[-1].content == "done"
        assert events[-1].provider_state["responses_output"][0]["type"] == "message"
        assert next(event for event in events if isinstance(event, UsageEvent)).usage.input_tokens == 11
        body = requests[0]
        assert body["store"] is False
        assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
        assert body["tools"] == [{"type": "function", "name": "echo", "description": "echo", "parameters": {"type": "object"}, "strict": False}]
        assert body["include"] == ["reasoning.encrypted_content"]
    finally:
        llm.client.close()


@pytest.mark.parametrize(
    ("status", "details", "expected"),
    [
        ("completed", None, "stop"),
        ("incomplete", {"reason": "max_output_tokens"}, "incomplete:max_output_tokens"),
        ("failed", None, "failed"),
        ("cancelled", None, "cancelled"),
    ],
)
def test_responses_terminal_status_is_not_invented(status, details, expected):
    llm = _responses_client(lambda _request: pytest.fail("SDK client is replaced"), stream=False)
    llm.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: SimpleNamespace(
        status=status, incomplete_details=details, error={"message": "bad"} if status == "failed" else None,
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="partial")])],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3),
    )))
    events = list(llm([{"role": "user", "content": "hello"}]))
    assert events[-1].finish_reason == expected
    assert events[-1].provider_state["responses_status"] == status


def test_responses_truncation_cannot_become_agent_final(tmp_path):
    class Model:
        context_limit = 10_000

        def __call__(self, _request, **_kwargs):
            yield ContentDone("partial answer", finish_reason="incomplete:max_output_tokens")

    session = Session.create("goal", tmp_path)
    assert Agent(Model(), [], session, SilentRenderer()).run("goal") is None
    assert session.current_run_status() == "failed"


def test_responses_stream_maps_text_usage_and_tool_continuation_state():
    llm = _responses_client(lambda _request: pytest.fail("SDK client is replaced"), stream=True)
    completed = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call", call_id="call_1", name="echo", arguments='{"value":"x"}',
            ),
        ],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2, total_tokens=5),
    )
    llm.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: iter([
        SimpleNamespace(type="response.output_text.delta", delta="checking"),
        SimpleNamespace(type="response.completed", response=completed),
    ])))

    events = list(llm([{"role": "user", "content": "hello"}]))

    assert events[0].piece == "checking"
    assert isinstance(events[1], UsageEvent)
    assert events[-1].tool_calls == [{
        "id": "call_1", "type": "function", "function": {"name": "echo", "arguments": '{"value":"x"}'},
    }]
    continuation = llm._responses_input([
        events[-1].assistant_message(),
        {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\":true}"},
    ])
    assert continuation[0]["type"] == "function_call"
    assert continuation[1] == {"type": "function_call_output", "call_id": "call_1", "output": '{"ok":true}'}


def test_mcp_alias_executes_original_tool_and_preserves_wire_name(tmp_path):
    from .responses import response

    original = "mcp__external.server__" + "long.tool." * 9
    observed = []
    tool = Tool(
        original,
        "external",
        {},
        lambda args, runtime: (
            observed.append(runtime.tool_name) or ToolResult.success("ok")
        ),
    )

    class Model:
        context_limit = 10000

        def __call__(self, messages, *, tools):
            name = tools[0]["name"]
            assert len(name) <= 64 and "." not in name
            if messages[-1]["role"] == "user":
                yield response(calls=[{"id": "external_call", "name": name}])
            else:
                assert messages[-2]["tool_calls"][0]["function"]["name"] == name
                yield ContentDone("done")

    session = Session.create("test", tmp_path)
    assert Agent(Model(), [tool], session, SilentRenderer()).run("test") == "done"
    assert observed == [original]
    assert session.tool_executions["external_call"].call.name == original


def test_invalid_batch_is_traced_without_executing_or_leaving_orphan_calls(tmp_path):
    from .responses import response

    invalid = response(calls=[{"name": "write"}, {"name": "write"}])
    invalid.tool_calls[1]["function"]["arguments"] = '{"incomplete":'

    class Model:
        context_limit = 10000

        def __call__(self, messages, **kwargs):
            if len(messages) == 2:
                yield invalid
            else:
                assert all(not message.get("tool_calls") for message in messages)
                yield ContentDone("unable to proceed")

    def write(args, runtime):
        raise AssertionError("Invalid batch must not execute")

    session = Session.create("test", tmp_path)
    assert (
        Agent(
            Model(), [Tool("write", "write", {}, write)], session, SilentRenderer()
        ).run("test")
        == "unable to proceed"
    )
    assert session.turns[0].route == "invalid"
    assert session.turns[0].parsed["response"]["tool_calls"] == invalid.tool_calls
    assert not session.tool_executions


def test_cancellation_after_model_response_closes_calls_for_resume(tmp_path):
    from .responses import response

    cancelled = False

    class Model:
        context_limit = 10000

        def __call__(self, messages, **kwargs):
            nonlocal cancelled
            cancelled = True
            yield response(calls=[{"id": "cancelled_call", "name": "write"}])

    def write(args, runtime):
        raise AssertionError("Cancelled call must not execute")

    session = Session.create("test", tmp_path)
    agent = Agent(
        Model(),
        [Tool("write", "write", {}, write)],
        session,
        SilentRenderer(),
        cancellation_check=lambda: cancelled,
    )
    assert agent.run("test") is None
    assert session.messages[-1]["role"] == "tool"
    assert session.messages[-1]["tool_call_id"] == "cancelled_call"
    assert session.tool_executions["cancelled_call"].result.ok is False
