"""Domain gateways (Ports) defining dependency inversion contracts."""

from __future__ import annotations

from .embedder import IEmbedder
from .knowledge_provider import IKnowledgeProvider, KnowledgeProvider
from .llm_gateway import ILLMGateway, ILLMProvider, LLMProvider
from .memory import (
    ICoreMemoryStore,
    IEpisodicMemoryStore,
    IFactRepository,
)
from .session_repository import ISessionRepository, IStorageGateway, SessionRepository
from .task_backend import TaskBackend
from .tool_executor import IToolExecutor
from .vector_store import IVectorStore

__all__ = [
    "ICoreMemoryStore",
    "IEmbedder",
    "IEpisodicMemoryStore",
    "IFactRepository",
    "IKnowledgeProvider",
    "ILLMGateway",
    "ILLMProvider",
    "ISessionRepository",
    "IStorageGateway",
    "IToolExecutor",
    "IVectorStore",
    "KnowledgeProvider",
    "LLMProvider",
    "SessionRepository",
    "TaskBackend",
]
