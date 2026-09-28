"""Chat-completions wire shape for a neutral model event."""

from __future__ import annotations

from typing import Any

from ...domain.model.llm.events import ContentDone


def assistant_message(done: ContentDone) -> dict[str, Any]:
    """Assemble the provider message stored with an assistant turn."""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": done.content or None,
    }
    if done.tool_calls:
        message["tool_calls"] = done.tool_calls
    if done.reasoning:
        message["reasoning_content"] = done.reasoning
    if done.provider_state:
        message["provider_state"] = done.provider_state
    return message


__all__ = ["assistant_message"]
