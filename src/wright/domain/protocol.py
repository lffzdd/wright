from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from .model.mcp import mcp_server_of
from .model.tool import ModelVisibleTool, ToolCall, ToolResult
from .model.tool_names import LEGACY_TOOL_NAMES

# Deferred tools beyond this many are not listed as stubs. Providers cap the
# number of functions per request (OpenAI: 128), and baseline tools need room.
DEFERRED_STUB_LIMIT = 64
STUB_PARAMETERS: dict = {"type": "object"}
_STUB_HINT = "Parameters are omitted here; call tool_search with this name before first use."


def build_tool_results_messages(
    tool_tuple: list[tuple[ToolCall, ToolResult]],
) -> list[dict]:
    """One native result per call, preserving the provider's call ID."""
    return [
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result.to_model_dict(), ensure_ascii=False),
        }
        for call, result in tool_tuple
    ]


def call_tool_only(
    tools: Sequence[ModelVisibleTool], limit: int = DEFERRED_STUB_LIMIT
) -> frozenset[str]:
    """Deferred tools that get no stub and run only through ``call_tool``.

    Built-in deferred tools take the stub slots first and MCP tools follow,
    each group in assembly order, so the overflow is the tail of MCP tools.
    """
    seen: dict[str, None] = {}
    for tool in tools:
        if tool.expose_to_model and tool.defer_to_model:
            seen.setdefault(tool.name)
    ordered = [name for name in seen if mcp_server_of(name) is None]
    ordered += [name for name in seen if mcp_server_of(name) is not None]
    return frozenset(ordered[limit:])


def _stub_description(description: str) -> str:
    line = description.strip().splitlines()[0] if description.strip() else ""
    match = re.match(r"(.+?[.!?\u3002])(\s|$)", line)
    sentence = match.group(1) if match else line
    if len(sentence) > 160:
        sentence = sentence[:157].rstrip() + "..."
    return f"{sentence} {_STUB_HINT}".strip()


def encode_tools(
    tools: Sequence[ModelVisibleTool],
) -> tuple[list[dict], dict[str, str]]:
    """Encode the fixed schema list sent with every request.

    Deferred tools are sent as stubs: their name, one sentence and an open
    object schema. The full schema reaches the model through ``tool_search``
    and is enforced by the dispatcher. Deferred tools past the stub limit are
    left out and run through ``call_tool``. Names get API-safe aliases without
    changing executor/permission names.
    """
    overflow = call_tool_only(tools)
    schemas = []
    names = {}
    for tool in tools:
        if not tool.expose_to_model or tool.name in overflow:
            continue
        name = tool.name
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) is None:
            prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:43]
            name = f"{prefix}_{hashlib.sha256(tool.name.encode()).hexdigest()[:20]}"
        if name in names:
            raise ValueError(f"Duplicate native tool name: {name}")
        names[name] = tool.name
        schema = {**tool.to_dict(), "name": name}
        if tool.defer_to_model:
            schema["description"] = _stub_description(schema["description"])
            schema["parameters"] = dict(STUB_PARAMETERS)
        elif not schema["parameters"]:
            schema["parameters"] = {"type": "object", "properties": {}}
        # Provider adapters own the Chat/Responses-specific function wrapper.
        schemas.append(schema)
    # Input-only: resumed histories still contain legacy names the model may
    # repeat. They are never advertised, so they cannot collide in ``tools``.
    exposed = set(names.values())
    for legacy, canonical in LEGACY_TOOL_NAMES.items():
        if canonical in exposed and legacy not in names:
            names[legacy] = canonical
    return schemas, names
