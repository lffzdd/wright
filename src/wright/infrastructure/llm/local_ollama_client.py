"""Local Ollama client adapter for offline/private LLMs."""

from __future__ import annotations

from typing import Any, Iterator, Sequence

from ...domain.gateway.llm_gateway import ILLMProvider
from .llm import LLMClient


class LocalOllamaClient(ILLMProvider):
    """Adapter for locally hosted Ollama instances."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        default_model: str = "llama3:latest",
    ) -> None:
        self.base_url = base_url
        self.default_model = default_model
        self._client = LLMClient(api_key="ollama", base_url=self.base_url)

    def __call__(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Any = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        return self._client(
            messages,
            tools=tools,
            model=model or self.default_model,
            **kwargs,
        )


__all__ = ["LocalOllamaClient"]
