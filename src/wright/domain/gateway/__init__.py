"""Domain gateways (Ports) defining dependency inversion contracts."""

from __future__ import annotations

from .embedder import IEmbedder
from .knowledge_provider import IKnowledgeProvider, KnowledgeProvider
from .llm_gateway import ILLMGateway, ILLMProvider, LLMProvider
from .memory import (
    ICoreMemoryStore,
    IEpisodeStore,
    ISemanticMemoryStore,
)
from .session_repository import ISessionRepository, IStorageGateway, SessionRepository
from .tool_executor import IToolExecutor
from .vector_store import IVectorStore

__all__ = [
    "ICoreMemoryStore",
    "IEmbedder",
    "IEpisodeStore",
    "IKnowledgeProvider",
    "ILLMGateway",
    "ILLMProvider",
    "ISemanticMemoryStore",
    "ISessionRepository",
    "IStorageGateway",
    "IToolExecutor",
    "IVectorStore",
    "KnowledgeProvider",
    "LLMProvider",
    "SessionRepository",
]
