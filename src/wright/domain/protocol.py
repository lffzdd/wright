from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from .model.tool import ModelVisibleTool, ToolCall, ToolResult


def build_tool_results_messages(
    tool_tuple: list[tuple[ToolCall, ToolResult]],
) -> list[dict]:
    """One native result per call, preserving the provider's call ID."""
    return [
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result.to_dict(), ensure_ascii=False),
        }
        for call, result in tool_tuple
    ]


def encode_tools(
    tools: Sequence[ModelVisibleTool], *, active_deferred: set[str] | None = None
) -> tuple[list[dict], dict[str, str]]:
    """Give MCP names API-safe aliases without changing executor/permission names."""
    schemas = []
    names = {}
    for tool in tools:
        if not tool.expose_to_model:
            continue
        if (
            active_deferred is not None
            and tool.defer_to_model
            and tool.name not in active_deferred
        ):
            continue
        name = tool.name
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) is None:
            prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:43]
            name = f"{prefix}_{hashlib.sha256(tool.name.encode()).hexdigest()[:20]}"
        if name in names:
            raise ValueError(f"Duplicate native tool name: {name}")
        names[name] = tool.name
        schema = {**tool.to_dict(), "name": name}
        if not schema["parameters"]:
            schema["parameters"] = {"type": "object", "properties": {}}
        # Provider adapters own the Chat/Responses-specific function wrapper.
        schemas.append(schema)
    return schemas, names
