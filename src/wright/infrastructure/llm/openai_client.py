"""OpenAI-compatible LLM client adapter (OpenAI, DeepSeek, Qwen, Moonshot)."""

from __future__ import annotations

from ...domain.model.llm.model_adapters import ChatAdapter, ResponsesAdapter
from .llm import LLMClient

# OpenAIClient is the canonical OpenAI-compatible implementation of ILLMGateway
OpenAIClient = LLMClient

__all__ = [
    "ChatAdapter",
    "LLMClient",
    "OpenAIClient",
    "ResponsesAdapter",
]
