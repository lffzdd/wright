import json

import httpx
import pytest
from openai import OpenAI

from ..agent import Agent
from ..events import ContentDone, UsageEvent
from ..llm import LLMClient
from ..memory.llm_util import side_query
from ..protocol import TurnAbort, parse_turn
from ..renderer import SilentRenderer
from ..session import SessionState
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
    session = SessionState.create("test", tmp_path)
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
            name = tools[0]["function"]["name"]
            assert len(name) <= 64 and "." not in name
            if messages[-1]["role"] == "user":
                yield response(calls=[{"id": "external_call", "name": name}])
            else:
                assert messages[-2]["tool_calls"][0]["function"]["name"] == name
                yield ContentDone("done")

    session = SessionState.create("test", tmp_path)
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

    session = SessionState.create("test", tmp_path)
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

    session = SessionState.create("test", tmp_path)
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
