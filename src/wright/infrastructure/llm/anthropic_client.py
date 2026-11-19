"""Native Anthropic Messages API transport.

``LLMClient`` routes the ``messages`` transport here.  Encoding lives in
``domain.model.llm.anthropic_adapter``; this module only talks to the SDK and
turns its stream into Wright's neutral events.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import anthropic

from ...domain.model.llm.anthropic_adapter import (
    MessagesAdapter,
    checks_thinking_binding,
    supports_adaptive_thinking,
)
from ...domain.model.llm.events import (
    ContentDelta,
    LLMEvent,
    ReasoningDelta,
    UsageEvent,
)

# Lets a request that replays thinking blocks after a client-side edit (the
# context compactor folding old tool results) drop those blocks instead of
# failing with a 400.
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
STREAM_MAX_TOKENS = 64_000
ONCE_MAX_TOKENS = 16_000


def sdk_base_url(base_url: str) -> str:
    """The SDK appends ``/v1/messages``; Wright's catalog URL ends in ``/v1/``."""
    trimmed = base_url.rstrip("/")
    return trimmed.removesuffix("/v1")


class AnthropicMessagesClient:
    """One endpoint and key; requests are stateless and safe to share."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        max_attempts: int = 3,
        http_client: Any = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        options: dict[str, Any] = {
            "api_key": api_key,
            "base_url": sdk_base_url(base_url),
            # The SDK retries 408/409/429/5xx and connection errors before the
            # first event, which is the same boundary LLMClient retries at.
            "max_retries": max(0, max_attempts - 1),
        }
        if http_client is not None:
            options["http_client"] = http_client
        self.client = anthropic.Anthropic(**options)

    def request_params(
        self,
        adapter: MessagesAdapter,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | None,
        cache_prefix_messages: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        encoded_tools = adapter.encode_tools(tools) if tools else []
        system, wire = adapter.encode_request(
            messages, cache_prefix_messages=cache_prefix_messages, tools=encoded_tools,
        )
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": STREAM_MAX_TOKENS if stream else ONCE_MAX_TOKENS,
            "messages": wire,
        }
        if system:
            params["system"] = system
        if encoded_tools:
            params["tools"] = encoded_tools
        if supports_adaptive_thinking(model):
            params["thinking"] = {"type": "adaptive", "display": "summarized"}
            if checks_thinking_binding(model):
                params["thinking"]["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
                params["betas"] = [THINKING_BINDING_BETA]
        schema = _json_schema(response_format)
        if schema is not None:
            params["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return params

    def call(self, adapter: MessagesAdapter, params: dict[str, Any], *, stream: bool) -> Iterator[LLMEvent]:
        if not stream:
            message = self.client.beta.messages.create(**params)
            done = adapter.decode_message(_dump(message))
            if getattr(message, "usage", None) is not None:
                yield UsageEvent(_dump(message.usage))
            yield done
            return
        with self.client.beta.messages.stream(**params) as events:
            for event in events:
                if getattr(event, "type", "") != "content_block_delta":
                    continue
                delta = event.delta
                kind = getattr(delta, "type", "")
                if kind == "text_delta" and delta.text:
                    yield ContentDelta(delta.text)
                elif kind == "thinking_delta" and delta.thinking:
                    yield ReasoningDelta(delta.thinking)
            message = events.get_final_message()
        if getattr(message, "usage", None) is not None:
            yield UsageEvent(_dump(message.usage))
        yield adapter.decode_message(_dump(message))


def _json_schema(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    """Only a JSON schema maps to structured output; ``json_object`` relies on the prompt."""
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return None
    inner = response_format.get("json_schema")
    schema = inner.get("schema") if isinstance(inner, dict) else response_format.get("schema")
    return schema if isinstance(schema, dict) else None


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


__all__ = ["AnthropicMessagesClient", "sdk_base_url"]
