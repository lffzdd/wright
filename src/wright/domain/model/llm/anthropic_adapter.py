"""Anthropic Messages API wire encoding.  Values are plain dicts, never SDK types.

The request keeps Wright's provider-neutral contract: the tools list is the
session's fixed list, and history is replayed append-only.  An assistant turn
that came from this API is replayed from ``provider_state["anthropic_content"]``
exactly as received, so thinking blocks keep their signatures.

Two ``cache_control`` breakpoints are placed: one on the last system block
(the tools render before it, so tools and system are cached together) and one
on the last block of the durable history, before this request's transient
reminders.  Earlier breakpoints from previous requests stay readable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any

from .events import ContentDone
from .model_adapters import _with_tool_images, normalize_tool_schema

CACHE_CONTROL = {"type": "ephemeral"}
# Blocks the API accepts a cache_control marker on.
_CACHEABLE = {"text", "image", "document", "tool_use", "tool_result"}
_DATA_URL = re.compile(r"^data:(?P<media>[^;,]+);base64,(?P<data>.*)$", re.DOTALL)
_MODEL_VERSION = re.compile(r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?(?:-|$)")

_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "refusal",
    "pause_turn": "incomplete:pause_turn",
}


def supports_adaptive_thinking(model: str) -> bool:
    """Claude 4.6 and later take ``thinking: {"type": "adaptive"}``.

    Older models (Opus/Sonnet 4.5, Haiku 4.5 and earlier) take budget-based
    thinking instead, so Wright sends no thinking setting to them.
    """
    version = _version(model)
    if version is None:
        return False
    family, major, minor = version
    if family in {"fable", "mythos"} or major >= 5:
        return True
    return major == 4 and minor >= 6 and family in {"opus", "sonnet"}


def checks_thinking_binding(model: str) -> bool:
    """Models that check a replayed thinking block's conversation prefix.

    Claude Fable/Mythos 5.1 and later, and Opus/Sonnet/Haiku 5.5 and later.
    Only these get ``thinking.block_binding``.
    """
    version = _version(model)
    if version is None:
        return False
    family, major, minor = version
    floor = (5, 1) if family in {"fable", "mythos"} else (5, 5)
    return (major, minor) >= floor


def _version(model: str) -> tuple[str, int, int] | None:
    match = _MODEL_VERSION.match(model.strip().lower().rsplit("/", 1)[-1])
    if match is None:
        return None
    return match["family"], int(match["major"]), int(match["minor"] or 0)


def stop_reason_to_finish(stop_reason: str | None) -> str:
    if not stop_reason:
        return "incomplete:missing_stop_reason"
    return _STOP_REASONS.get(stop_reason, f"incomplete:{stop_reason}")


class MessagesAdapter:
    """Encode internal conversation values for the Anthropic Messages API."""

    def __init__(
        self, attachment_data_url: Callable[[str], str],
        artifact_data_url: Callable[[dict], str] | None = None,
    ) -> None:
        self._attachment_data_url = attachment_data_url
        self._artifact_data_url = artifact_data_url

    def encode_tools(self, tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tools in the given order; arguments are validated locally, not with ``strict``."""
        return [
            {
                "name": item["name"],
                "description": item["description"],
                "input_schema": item["parameters"],
            }
            for item in (normalize_tool_schema(tool) for tool in tools)
        ]

    def encode_request(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        cache_prefix_messages: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return ``(system, messages)`` with cache breakpoints placed.

        ``cache_prefix_messages`` counts the leading internal messages that
        are durable history; ``None`` treats every message as durable.  When
        ``tools`` is given and there is no system prompt, the last tool
        definition carries the first breakpoint instead.
        """
        system: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        # For each internal message index: how many wire blocks existed after it.
        ends: list[tuple[int, int]] = []
        # Tool images arrive as request-only user messages; they belong to no index.
        sources = {id(message) for message in messages}
        for raw in _with_tool_images(messages, self._artifact_data_url):
            projected = id(raw) not in sources
            role = str(raw.get("role", "user"))
            if role == "system" and not turns:
                text = _text(raw.get("content"))
                if text:
                    system.append({"type": "text", "text": text})
            elif role == "assistant":
                _append(turns, "assistant", self._assistant_blocks(raw))
            elif role == "tool":
                _append(turns, "user", [{
                    "type": "tool_result",
                    "tool_use_id": str(raw.get("tool_call_id", "")),
                    "content": _text(raw.get("content")),
                }])
            else:
                # Later system messages (none today) stay operator text in the user turn.
                _append(turns, "user", self._user_blocks(raw))
            if not projected:
                ends.append((len(turns), len(turns[-1]["content"]) if turns else 0))
        if system:
            system[-1]["cache_control"] = dict(CACHE_CONTROL)
        elif tools:
            tools[-1]["cache_control"] = dict(CACHE_CONTROL)
        durable = len(ends) if cache_prefix_messages is None else min(cache_prefix_messages, len(ends))
        if durable > 0:
            _mark_last_cacheable(turns, *ends[durable - 1])
        return system, turns

    def _assistant_blocks(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        state = raw.get("provider_state")
        if isinstance(state, dict) and isinstance(state.get("anthropic_content"), list):
            return deepcopy(state["anthropic_content"])
        blocks: list[dict[str, Any]] = []
        text = _text(raw.get("content"))
        if text:
            blocks.append({"type": "text", "text": text})
        for call in raw.get("tool_calls") or []:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
            except (TypeError, ValueError):
                arguments = {}
            blocks.append({
                "type": "tool_use",
                "id": str(call.get("id", "")),
                "name": str(call["function"].get("name", "")),
                "input": arguments if isinstance(arguments, dict) else {},
            })
        return blocks

    def _user_blocks(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        parts = raw.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    blocks.append({"type": "text", "text": str(part["text"])})
                elif part.get("type") == "image":
                    url = part.get("data_url") or self._attachment_data_url(str(part.get("attachment_id", "")))
                    blocks.append(_image_block(str(url)))
        if not blocks:
            text = _text(raw.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
        return blocks

    def decode_message(self, message: dict[str, Any]) -> ContentDone:
        """Map one complete Messages response (as a dict) to the neutral terminal event."""
        content: list[str] = []
        reasoning: list[str] = []
        calls: list[dict[str, Any]] = []
        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                content.append(str(block.get("text", "")))
            elif kind == "thinking":
                reasoning.append(str(block.get("thinking", "")))
            elif kind == "tool_use":
                calls.append({
                    "id": str(block.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })
        stop_reason = message.get("stop_reason")
        finish = stop_reason_to_finish(stop_reason)
        state: dict[str, Any] = {
            "anthropic_content": deepcopy(blocks),
            "anthropic_model": str(message.get("model", "")),
            "anthropic_stop_reason": str(stop_reason or ""),
        }
        if isinstance(message.get("stop_details"), dict):
            state["anthropic_stop_details"] = deepcopy(message["stop_details"])
        return ContentDone(
            content="".join(content),
            reasoning="".join(reasoning),
            tool_calls=calls,
            finish_reason=finish,
            provider_state=state,
        )


def has_anthropic_state(message: dict[str, Any]) -> bool:
    state = message.get("provider_state")
    return isinstance(state, dict) and "anthropic_content" in state


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _image_block(url: str) -> dict[str, Any]:
    match = _DATA_URL.match(url)
    if match is None:
        return {"type": "image", "source": {"type": "url", "url": url}}
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": match["media"], "data": match["data"]},
    }


def _append(turns: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """Add blocks, merging into the previous turn of the same role.

    Tool results and the tool-image message that follows them become one user
    turn, which is also what the API would do with consecutive user turns.
    """
    if not blocks:
        return
    if turns and turns[-1]["role"] == role:
        turns[-1]["content"].extend(blocks)
    else:
        turns.append({"role": role, "content": blocks})


def _mark_last_cacheable(turns: list[dict[str, Any]], turn_count: int, block_count: int) -> None:
    """Mark the last cacheable block at or before the given position."""
    for turn_index in range(turn_count - 1, -1, -1):
        blocks = turns[turn_index]["content"]
        start = block_count if turn_index == turn_count - 1 else len(blocks)
        for block in reversed(blocks[:start]):
            if block.get("type") in _CACHEABLE:
                block["cache_control"] = dict(CACHE_CONTROL)
                return


__all__ = [
    "CACHE_CONTROL",
    "MessagesAdapter",
    "checks_thinking_binding",
    "has_anthropic_state",
    "stop_reason_to_finish",
    "supports_adaptive_thinking",
]
