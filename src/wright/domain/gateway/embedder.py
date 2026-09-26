"""Domain gateway port for dense vector embedding models."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class IEmbedder(Protocol):
    """Port for computing dense vector representations of text."""

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Compute embeddings for a batch of text chunks."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Compute embedding for a single user query."""
        ...


__all__ = ["IEmbedder"]
