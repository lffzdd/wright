"""Domain gateway port for vector storage and similarity search."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..model.knowledge import DocumentChunk


@runtime_checkable
class IVectorStore(Protocol):
    """Port for indexing and searching document vector embeddings."""

    def add(self, chunks: Sequence[DocumentChunk]) -> None:
        """Add document chunks with their embeddings to the store."""
        ...

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
    ) -> list[tuple[DocumentChunk, float]]:
        """Search top-k most similar document chunks given a query embedding."""
        ...

    def save(self, path: Path | str) -> None:
        """Persist the vector store to disk."""
        ...

    def load(self, path: Path | str) -> bool:
        """Load the vector store from disk. Returns True if successfully loaded."""
        ...

    def __len__(self) -> int:
        """Return total number of chunks currently stored."""
        ...


__all__ = ["IVectorStore"]
