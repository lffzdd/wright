"""Domain gateway port for knowledge retrieval providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..model.knowledge import KnowledgeHit


@runtime_checkable
class KnowledgeProvider(Protocol):
    """Port for knowledge base retrieval."""

    def search(self, query: str, top_k: int) -> list[KnowledgeHit]:
        """返回项目内的命中列表；不可用时抛 KnowledgeUnavailable。"""
        ...


__all__ = ["KnowledgeProvider"]
