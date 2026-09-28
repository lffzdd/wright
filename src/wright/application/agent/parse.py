"""Turn parsing. Wire-message assembly stays in the LLM adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from ...domain.model.llm.events import ContentDone
from ...domain.model.tool import ToolCall
from ...infrastructure.llm.wire import assistant_message


class TurnAbort(Exception):
    """An incomplete or invalid provider response cannot be executed."""


@dataclass
class ParsedTurn:
    kind: Literal["final", "tool_calls"]
    parsed: dict
    assistant_message: dict
    final_answer: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


def parse_turn(response: ContentDone) -> ParsedTurn:
    if response.finish_reason not in {None, "stop", "tool_calls"}:
        raise TurnAbort(f"Response did not complete: {response.finish_reason}")

    message = assistant_message(response)
    calls: list[ToolCall] = []
    ids: set[str] = set()
    for item in response.tool_calls:
        if not isinstance(item, dict) or item.get("type") != "function":
            raise TurnAbort("Expected a native function tool call")
        call_id = item.get("id")
        function = item.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in ids:
            raise TurnAbort("Tool call IDs must be non-empty and unique")
        if (
            not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"]
        ):
            raise TurnAbort(f"Missing function name for {call_id}")
        try:
            arguments = json.loads(function["arguments"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise TurnAbort(
                f"Invalid JSON arguments for {function['name']}: {exc}"
            ) from exc
        if not isinstance(arguments, dict):
            raise TurnAbort(f"Arguments for {function['name']} must be an object")
        ids.add(call_id)
        calls.append(ToolCall(function["name"], arguments, call_id))

    parsed = {
        "content": response.content,
        "reasoning": response.reasoning,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in calls
        ],
        "final_answer": None if calls else response.content,
        "finish_reason": response.finish_reason,
    }
    if calls:
        return ParsedTurn("tool_calls", parsed, message, tool_calls=calls)
    if response.finish_reason == "tool_calls" or not response.content.strip():
        raise TurnAbort("Response contains neither tool calls nor a final answer")
    return ParsedTurn("final", parsed, message, final_answer=response.content)


__all__ = ["ParsedTurn", "TurnAbort", "parse_turn"]
