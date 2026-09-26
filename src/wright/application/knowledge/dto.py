"""Data Transfer Objects for RAG knowledge operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IngestDocumentRequest:
    """Request payload to index a document content into the knowledge base."""

    content: str
    source: str = ""
    document_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    chunk_size: int = 1000
    chunk_overlap: int = 100


@dataclass(frozen=True)
class IngestResultDTO:
    """Result of an ingestion operation."""

    document_id: str
    chunks_count: int
    source: str = ""


__all__ = ["IngestDocumentRequest", "IngestResultDTO"]
