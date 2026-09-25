"""OpenAI-compatible LLM client adapter (OpenAI, DeepSeek, Qwen, Moonshot)."""

from __future__ import annotations

from .llm import LLMClient
from .model_adapters import ChatAdapter, ResponsesAdapter

# OpenAIClient is the canonical OpenAI-compatible implementation of ILLMGateway
OpenAIClient = LLMClient

__all__ = [
    "ChatAdapter",
    "LLMClient",
    "OpenAIClient",
    "ResponsesAdapter",
]
