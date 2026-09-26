"""KnowledgeService: Application use-case orchestrator for RAG indexing and retrieval."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...domain.gateway.embedder import IEmbedder
from ...domain.gateway.knowledge_provider import IKnowledgeProvider
from ...domain.gateway.vector_store import IVectorStore
from ...domain.model.knowledge import DocumentChunk, KnowledgeHit
from ...utils.text_splitter import split_text
from .dto import IngestDocumentRequest, IngestResultDTO


class KnowledgeService(IKnowledgeProvider):
    """Orchestrates document chunking, embedding computation, vector indexing, and search."""

    def __init__(
        self,
        vector_store: IVectorStore,
        embedder: IEmbedder,
        splitter: Callable[..., list[str]] | None = None,
    ) -> None:
        self.vector_store = vector_store
        self.embedder = embedder
        self.splitter = splitter or split_text

    def ingest_document(self, request: IngestDocumentRequest) -> IngestResultDTO:
        """Split text, compute dense embeddings, and index into the vector store."""
        content = request.content.strip()
        if not content:
            return IngestResultDTO(document_id=request.document_id, chunks_count=0, source=request.source)

        chunks_text = self.splitter(
            content,
            chunk_size=request.chunk_size,
            chunk_overlap=request.chunk_overlap,
        )
        if not chunks_text:
            return IngestResultDTO(document_id=request.document_id, chunks_count=0, source=request.source)

        doc_id = (
            request.document_id
            or request.source
            or f"doc-{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}"
        )

        embeddings = self.embedder.embed_texts(chunks_text)
        total_chunks = len(chunks_text)

        document_chunks: list[DocumentChunk] = []
        for idx, (chunk_str, emb) in enumerate(zip(chunks_text, embeddings)):
            chunk_meta = dict(request.metadata)
            chunk_meta.update({
                "source": request.source or doc_id,
                "document_id": doc_id,
                "chunk_index": idx,
                "chunk_total": total_chunks,
            })
            chunk_id = f"{doc_id}-chunk-{idx}"
            document_chunks.append(
                DocumentChunk(
                    id=chunk_id,
                    content=chunk_str,
                    metadata=chunk_meta,
                    embedding=tuple(emb),
                )
            )

        self.vector_store.add(document_chunks)
        return IngestResultDTO(
            document_id=doc_id,
            chunks_count=total_chunks,
            source=request.source,
        )

    def ingest_file(
        self,
        file_path: Path | str,
        *,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        extra_metadata: dict[str, Any] | None = None,
    ) -> IngestResultDTO:
        """Read a file from disk and ingest its text content."""
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"File not found for indexing: {path}")

        content = path.read_text(encoding="utf-8", errors="replace")
        meta = {
            "filename": path.name,
            "filepath": str(path),
            **(extra_metadata or {}),
        }
        return self.ingest_document(
            IngestDocumentRequest(
                content=content,
                source=path.name,
                document_id=path.name,
                metadata=meta,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        )

    def search(self, query: str, top_k: int = 3) -> list[KnowledgeHit]:
        """Search top-k most relevant document chunks for the given query."""
        trimmed_query = query.strip()
        if not trimmed_query:
            return []

        query_vector = self.embedder.embed_query(trimmed_query)
        scored_chunks = self.vector_store.search(query_vector, top_k=top_k)

        hits: list[KnowledgeHit] = []
        for chunk, score in scored_chunks:
            meta = chunk.metadata or {}
            hits.append(
                KnowledgeHit(
                    content=chunk.content,
                    score=float(score),
                    source=str(meta.get("source") or meta.get("filename") or ""),
                    document_id=str(meta.get("document_id") or meta.get("doc_id") or ""),
                    filename=str(meta.get("filename") or ""),
                    filepath=str(meta.get("filepath") or ""),
                    chunk_index=meta.get("chunk_index") if isinstance(meta.get("chunk_index"), int) else None,
                    chunk_total=meta.get("chunk_total") if isinstance(meta.get("chunk_total"), int) else None,
                    page=meta.get("page") if isinstance(meta.get("page"), int) else None,
                )
            )
        return hits

    def save_index(self, path: Path | str) -> None:
        """Persist vector index to file."""
        self.vector_store.save(path)

    def load_index(self, path: Path | str) -> bool:
        """Load vector index from file."""
        return self.vector_store.load(path)


__all__ = ["KnowledgeService"]
