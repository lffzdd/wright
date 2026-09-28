"""Token estimation and counting utilities."""

from __future__ import annotations

import json

from ..common.constants import CHARS_PER_TOKEN


def estimate_tokens(text: str) -> int:
    """Roughly estimate token count based on character length."""
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


def tool_image_references(message: dict) -> list[dict]:
    """Read managed image references from a native tool-result envelope."""
    if message.get("role") != "tool" or not isinstance(message.get("content"), str):
        return []
    try:
        result = json.loads(message["content"])
    except (ValueError, TypeError):
        return []
    refs = result.get("artifacts") if isinstance(result, dict) else None
    if not isinstance(refs, list):
        return []
    return [
        ref
        for ref in refs
        if isinstance(ref, dict) and str(ref.get("media_type", "")).startswith("image/")
    ]


def estimate_message_tokens(message: dict) -> int:
    """Estimate token count for a message including content, tool calls, and images."""
    content = message.get("content")
    count = estimate_tokens(content) if isinstance(content, str) else 0
    if message.get("tool_calls"):
        count += estimate_tokens(json.dumps(message["tool_calls"], ensure_ascii=False))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        count += estimate_tokens(reasoning)

    parts = message.get("parts")
    if isinstance(parts, list):
        count += 1024 * sum(
            isinstance(part, dict) and part.get("type") == "image"
            for part in parts
        )
    count += 1024 * len(tool_image_references(message))
    return count


def estimate_tools_tokens(tools: tuple[dict, ...] | list[dict]) -> int:
    """Estimate request schema input separately from history and actual usage."""
    return estimate_tokens(json.dumps(tools, ensure_ascii=False, sort_keys=True))
