"""Anthropic Claude LLM client adapter."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from ...domain.gateway.llm_gateway import ILLMProvider
from .llm import LLMClient


class AnthropicClient(ILLMProvider):
    """Adapter for Anthropic Claude native API protocol."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        default_model: str = "claude-3-5-sonnet-20241022",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = default_model

    def __call__(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Any = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        # Delegates to the compatible runtime client with Anthropic model config
        client = LLMClient(api_key=self.api_key, base_url=self.base_url)
        return client(messages, tools=tools, model=model or self.default_model, **kwargs)


__all__ = ["AnthropicClient"]
