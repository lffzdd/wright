"""Native response fixtures shared by offline agent and orchestration tests."""

import json
from uuid import uuid4

from wright.events import ContentDone


def response(*, content=None, calls=(), reasoning=""):
    return ContentDone(
        content=content or "",
        reasoning=reasoning,
        tool_calls=[
            {
                "id": call.get("id") or f"call_{uuid4().hex}",
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(
                        call.get("arguments", {}), ensure_ascii=False
                    ),
                },
            }
            for call in calls
        ],
        finish_reason="tool_calls" if calls else "stop",
    )


def event(content="", reasoning=""):
    """Scripts can contain native responses or plain text side-query results."""
    if isinstance(content, ContentDone):
        return content
    return ContentDone(content, reasoning)
