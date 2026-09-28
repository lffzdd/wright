"""Display values for the fullscreen transcript.

Widgets and the renderer both consume these values. This module does not
import the renderer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def tool_call_name(tool_call: Any) -> str:
    name = getattr(tool_call, "name", None)
    if not name and isinstance(tool_call, dict):
        name = tool_call.get("name")
    return str(name or "tool")


def tool_call_id(tool_call: Any) -> str | None:
    call_id = getattr(tool_call, "id", None)
    if not call_id and isinstance(tool_call, dict):
        call_id = tool_call.get("id")
    return str(call_id) if call_id else None


def tool_call_args(tool_call: Any) -> Any:
    arguments = getattr(tool_call, "arguments", None)
    if arguments is None and isinstance(tool_call, dict):
        arguments = tool_call.get("arguments")
    return arguments


def stringify_answer(answer: Any) -> str:
    if answer is None:
        return ""
    if isinstance(answer, str):
        return answer
    try:
        return json.dumps(answer, ensure_ascii=False, indent=2)
    except Exception:
        return str(answer)


@dataclass
class ToolView:
    key: str
    name: str
    arguments: Any = None
    status: str = "planned"  # planned | awaiting_approval | running | done | error
    result: Any = None
    error: str = ""
    output: str = ""
