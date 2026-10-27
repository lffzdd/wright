"""OpenAI-compatible servers stream tool calls in several shapes."""

from types import SimpleNamespace as NS

import httpx
import pytest
from openai import APIStatusError

from wright.application.agent.parse import TurnAbort, parse_turn
from wright.domain.model.llm.events import ContentDone
from wright.infrastructure.llm.llm import describe_model_error, merge_tool_call_delta


def frag(index=None, id=None, name=None, arguments=None):
    return NS(index=index, id=id, type="function" if id else None,
              function=NS(name=name, arguments=arguments))


def fold(fragments):
    calls = {}
    for item in fragments:
        merge_tool_call_delta(calls, item)
    return [calls[key] for key in sorted(calls)]


def test_openai_shape_is_unchanged():
    calls = fold([
        frag(0, "call_1", "read_file", ""),
        frag(0, None, None, '{"fi'),
        frag(0, None, None, 'le": "a"}'),
    ])
    assert calls[0]["id"] == "call_1"
    assert calls[0]["function"] == {"name": "read_file", "arguments": '{"file": "a"}'}


def test_repeated_id_and_name_are_not_concatenated():
    calls = fold([
        frag(0, "call_1", "list_directory", ""),
        frag(0, "call_1", "list_directory", "{"),
        frag(0, "call_1", "list_directory", "}"),
    ])
    assert calls[0]["id"] == "call_1"
    assert calls[0]["function"] == {"name": "list_directory", "arguments": "{}"}


def test_missing_index_keeps_parallel_calls_apart():
    calls = fold([
        frag(None, "a", "read_file", '{"file": "x"}'),
        frag(None, "b", "list_directory", "{}"),
    ])
    assert [c["function"]["name"] for c in calls] == ["read_file", "list_directory"]
    assert [c["id"] for c in calls] == ["a", "b"]


def test_cumulative_argument_resend_is_not_duplicated():
    calls = fold([frag(0, "c", "grep", '{"pat'), frag(0, None, None, '{"pattern": "x"}')])
    assert calls[0]["function"]["arguments"] == '{"pattern": "x"}'


@pytest.mark.parametrize("raw", ["", "  ", None])
def test_empty_arguments_mean_an_empty_object(raw):
    done = ContentDone("", tool_calls=[{"id": "c", "type": "function", "function": {"name": "get_plan", "arguments": raw}}],
                       finish_reason="tool_calls")
    turn = parse_turn(done)
    assert turn.tool_calls[0].arguments == {}


def test_malformed_arguments_still_abort():
    done = ContentDone("", tool_calls=[{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{oops"}}],
                       finish_reason="tool_calls")
    with pytest.raises(TurnAbort):
        parse_turn(done)


def test_model_errors_read_as_one_line():
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    error = APIStatusError(
        "Error code: 400", response=httpx.Response(400, request=request),
        body={"error": {"message": "context length exceeded", "type": "invalid_request_error"}},
    )
    assert describe_model_error(error) == "Model request failed (HTTP 400): context length exceeded"
