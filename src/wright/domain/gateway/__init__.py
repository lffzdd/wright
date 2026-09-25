"""Domain gateways (Ports) defining dependency inversion contracts."""

from __future__ import annotations

from .knowledge_provider import KnowledgeProvider
from .llm_gateway import ILLMGateway, ILLMProvider, LLMProvider
from .memory import (
    ICoreMemoryStore,
    IEpisodicMemoryStore,
    IFactRepository,
    IMemoryStore,
)
from .session_repository import ISessionRepository, IStorageGateway, SessionRepository
from .task_backend import TaskBackend
from .tool_executor import IToolExecutor, IToolGateway, ToolExecutorPort

__all__ = [
    "ICoreMemoryStore",
    "IEpisodicMemoryStore",
    "IFactRepository",
    "ILLMGateway",
    "ILLMProvider",
    "IMemoryStore",
    "ISessionRepository",
    "IStorageGateway",
    "IToolExecutor",
    "IToolGateway",
    "KnowledgeProvider",
    "LLMProvider",
    "SessionRepository",
    "TaskBackend",
    "ToolExecutorPort",
]
