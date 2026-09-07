import pytest

from ..events import ContentDone
from ..protocol import TurnAbort, parse_turn
from .responses import response


def test_natural_text_and_json_answers_are_not_control_protocols():
    text = '{"tool_calls": [], "final_answer": "a literal JSON example"}'
    turn = parse_turn(ContentDone(text))
    assert turn.final_answer == text
    assert turn.assistant_message == {"role": "assistant", "content": text}


def test_content_and_multiple_calls_preserve_provider_ids_and_arguments():
    reply = response(
        content="I will inspect both files",
        calls=[
            {
                "id": "provider_a",
                "name": "read_file",
                "arguments": {"file": "中文.txt"},
            },
            {"id": "provider_b", "name": "read_file", "arguments": {"file": "b.txt"}},
        ],
    )
    turn = parse_turn(reply)
    assert turn.kind == "tool_calls"
    assert turn.final_answer is None
    assert [call.id for call in turn.tool_calls] == ["provider_a", "provider_b"]
    assert turn.tool_calls[0].arguments == {"file": "中文.txt"}
    assert turn.assistant_message == reply.assistant_message()
    assert turn.parsed["content"] == "I will inspect both files"


@pytest.mark.parametrize(
    "arguments", ['{"file":"a"', '{"file":"a",}', "[]", "null", '"x"']
)
def test_invalid_arguments_are_never_repaired(arguments):
    reply = response(calls=[{"name": "write_file"}])
    reply.tool_calls[0]["function"]["arguments"] = arguments
    with pytest.raises(TurnAbort):
        parse_turn(reply)


@pytest.mark.parametrize("finish", ["length", "content_filter", "incomplete"])
def test_partial_tool_calls_cannot_execute(finish):
    reply = response(calls=[{"name": "write_file"}])
    reply.finish_reason = finish
    with pytest.raises(TurnAbort, match="did not complete"):
        parse_turn(reply)


def test_duplicate_ids_empty_response_and_missing_calls_are_invalid():
    reply = response(calls=[{"id": "same", "name": "a"}, {"id": "same", "name": "b"}])
    for item in [
        reply,
        ContentDone(""),
        ContentDone("text", finish_reason="tool_calls"),
    ]:
        with pytest.raises(TurnAbort):
            parse_turn(item)
