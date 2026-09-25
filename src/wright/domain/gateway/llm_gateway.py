"""LLM gateway port definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any


class LLMProvider(ABC):
    """Port for Large Language Model completions and streaming."""

    @abstractmethod
    def __call__(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Any = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Generate completion events for given messages and tools."""
        ...


# Interface compatibility aliases
ILLMProvider = LLMProvider
ILLMGateway = LLMProvider

__all__ = ["ILLMGateway", "ILLMProvider", "LLMProvider"]
