"""Route native API responses into executable calls or a final answer.

ParsedTurn is an internal execution/trace record, never an output schema imposed
on the model. Tool arguments are decoded strictly; truncated output is not repaired.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from .events import ContentDone
from .tools.base import Tool, ToolCall


def encode_tools(tools: list[Tool]) -> tuple[list[dict], dict[str, str]]:
    """Give MCP names API-safe aliases without changing executor/permission names."""
    schemas = []
    names = {}
    for tool in tools:
        if not tool.expose_to_model:
            continue
        name = tool.name
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) is None:
            prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:43]
            name = f"{prefix}_{hashlib.sha256(tool.name.encode()).hexdigest()[:20]}"
        if name in names:
            raise ValueError(f"Duplicate native tool name: {name}")
        names[name] = tool.name
        function = {**tool.to_dict(), "name": name}
        if not function["parameters"]:
            function["parameters"] = {"type": "object", "properties": {}}
        schemas.append({"type": "function", "function": function})
    return schemas, names


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

    message = response.assistant_message()
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

    # These fields serve history, verification and replay. The model does not
    # generate this envelope; content accompanying tool calls is kept as content.
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
