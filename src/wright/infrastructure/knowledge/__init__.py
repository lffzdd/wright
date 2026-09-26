"""Knowledge infrastructure adapters, providers, and tools."""

from __future__ import annotations

from .embedder import ApiEmbedder
from .rag_provider import (
    RagKnowledgeProvider,
    knowledge_enabled,
    knowledge_index_path,
)
from .tools import build_knowledge_tools
from .vector_store import SimpleVectorStore


def optional_knowledge_tools(*, enabled: bool | None = None):
    """未显式启用时返回空列表，避免不可用工具占住每个新会话。"""
    is_active = enabled if enabled is not None else knowledge_enabled()
    if not is_active:
        return []

    return build_knowledge_tools(RagKnowledgeProvider.from_env())


__all__ = [
    "ApiEmbedder",
    "RagKnowledgeProvider",
    "SimpleVectorStore",
    "build_knowledge_tools",
    "knowledge_enabled",
    "knowledge_index_path",
    "optional_knowledge_tools",
]
