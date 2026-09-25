"""Knowledge infrastructure adapters and tools."""

from __future__ import annotations

from .rag_provider import (
    RagKnowledgeProvider,
    knowledge_enabled,
    knowledge_index_path,
)


def optional_knowledge_tools():
    """未显式启用时返回空列表，避免不可用工具占住每个新会话。"""
    if not knowledge_enabled():
        return []
    from ..tools.knowledge_tools import build_knowledge_tools

    return build_knowledge_tools(RagKnowledgeProvider.from_env())


__all__ = [
    "RagKnowledgeProvider",
    "knowledge_enabled",
    "knowledge_index_path",
    "optional_knowledge_tools",
]
