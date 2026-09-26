"""Simple in-memory and JSON-persisted vector store with cosine similarity search."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

from ....domain.gateway.vector_store import IVectorStore
from ....domain.model.knowledge import DocumentChunk


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    """Compute cosine similarity between two float vectors."""
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0

    dot = 0.0
    norm_a_sq = 0.0
    norm_b_sq = 0.0

    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a_sq += a * a
        norm_b_sq += b * b

    if norm_a_sq <= 0.0 or norm_b_sq <= 0.0:
        return 0.0

    return dot / (math.sqrt(norm_a_sq) * math.sqrt(norm_b_sq))


class SimpleVectorStore(IVectorStore):
    """Pure-Python thread-safe vector store persisting to simple_index.json format."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._chunks: list[DocumentChunk] = []
        self._vectors: list[list[float]] = []

    def add(self, chunks: Sequence[DocumentChunk]) -> None:
        """Add document chunks to the in-memory store."""
        with self._lock:
            for chunk in chunks:
                self._chunks.append(chunk)
                self._vectors.append(list(chunk.embedding))

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
    ) -> list[tuple[DocumentChunk, float]]:
        """Search top-k most similar chunks by cosine similarity."""
        with self._lock:
            if not self._chunks or not query_vector:
                return []

            scored: list[tuple[DocumentChunk, float]] = []
            for chunk, vec in zip(self._chunks, self._vectors):
                score = _cosine_similarity(query_vector, vec)
                scored.append((chunk, score))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    def save(self, path: Path | str) -> None:
        """Atomically persist vectors and chunk metadata to a JSON file."""
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            chunks_data = [
                {
                    "id": c.id,
                    "content": c.content,
                    "metadata": c.metadata,
                }
                for c in self._chunks
            ]
            payload = json.dumps(
                {"vectors": self._vectors, "chunks": chunks_data},
                ensure_ascii=False,
                indent=2,
            ) + "\n"

            descriptor, tmp_path = tempfile.mkstemp(
                prefix=".simple_index.",
                suffix=".tmp",
                dir=target.parent,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, target)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

    def load(self, path: Path | str) -> bool:
        """Load vectors and chunks from a JSON index file."""
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            return False

        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            vectors = data.get("vectors", [])
            raw_chunks = data.get("chunks", [])
            if not isinstance(vectors, list) or not isinstance(raw_chunks, list):
                return False

            loaded_chunks: list[DocumentChunk] = []
            loaded_vectors: list[list[float]] = []

            for idx, raw in enumerate(raw_chunks):
                if not isinstance(raw, dict):
                    continue
                content = str(raw.get("content", ""))
                meta = raw.get("metadata", {})
                if not isinstance(meta, dict):
                    meta = {}
                chunk_id = str(raw.get("id") or f"chunk-{idx}")
                vec = vectors[idx] if idx < len(vectors) else []
                loaded_chunks.append(
                    DocumentChunk(
                        id=chunk_id,
                        content=content,
                        metadata=meta,
                        embedding=tuple(vec),
                    )
                )
                loaded_vectors.append(list(vec))

            with self._lock:
                self._chunks = loaded_chunks
                self._vectors = loaded_vectors
            return True
        except Exception:
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._chunks)


__all__ = ["SimpleVectorStore"]
