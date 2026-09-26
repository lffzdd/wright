"""Tests for standard RAG architecture: Embedder, SimpleVectorStore, and KnowledgeService."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wright.application.knowledge import IngestDocumentRequest, KnowledgeService
from wright.domain.gateway.embedder import IEmbedder
from wright.domain.gateway.knowledge_provider import IKnowledgeProvider
from wright.domain.gateway.vector_store import IVectorStore
from wright.domain.model.knowledge import DocumentChunk, KnowledgeHit
from wright.infrastructure.knowledge import ApiEmbedder, SimpleVectorStore
from wright.infrastructure.tools.knowledge import build_knowledge_tools
from wright.utils.text_splitter import split_text


class MockEmbedder(IEmbedder):
    """Deterministic mock embedder assigning predictable unit vectors."""

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {
            "apple": 0,
            "banana": 1,
            "agent": 2,
            "react": 3,
        }

    def _embed(self, text: str) -> list[float]:
        vec = [0.0, 0.0, 0.0, 0.0]
        lower = text.lower()
        for word, idx in self.vocab.items():
            if word in lower:
                vec[idx] += 1.0
        # If nothing matches, assign uniform background
        if sum(vec) == 0:
            return [0.25, 0.25, 0.25, 0.25]
        # Normalize
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def test_text_splitter_basic_and_overlap():
    text = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
    chunks = split_text(text, chunk_size=15, chunk_overlap=5)
    assert len(chunks) >= 2
    assert all(len(c) <= 25 for c in chunks)


def test_simple_vector_store_add_search_save_load(tmp_path: Path):
    store = SimpleVectorStore()
    assert isinstance(store, IVectorStore)
    assert len(store) == 0

    c1 = DocumentChunk(
        id="c1",
        content="Apple pie recipe",
        metadata={"category": "food", "source": "recipes.md"},
        embedding=(1.0, 0.0, 0.0, 0.0),
    )
    c2 = DocumentChunk(
        id="c2",
        content="Banana bread recipe",
        metadata={"category": "food", "source": "recipes.md"},
        embedding=(0.0, 1.0, 0.0, 0.0),
    )
    c3 = DocumentChunk(
        id="c3",
        content="Agent architecture in python",
        metadata={"category": "tech", "source": "agent.md"},
        embedding=(0.0, 0.0, 1.0, 0.0),
    )

    store.add([c1, c2, c3])
    assert len(store) == 3

    # Query for apple
    results = store.search([1.0, 0.0, 0.0, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0][0].id == "c1"
    assert results[0][1] == pytest.approx(1.0)

    # Save to disk
    index_file = tmp_path / "simple_index.json"
    store.save(index_file)
    assert index_file.is_file()

    # Load into fresh store
    store2 = SimpleVectorStore()
    loaded = store2.load(index_file)
    assert loaded is True
    assert len(store2) == 3

    results2 = store2.search([0.0, 1.0, 0.0, 0.0], top_k=1)
    assert len(results2) == 1
    assert results2[0][0].id == "c2"


def test_knowledge_service_end_to_end(tmp_path: Path):
    store = SimpleVectorStore()
    embedder = MockEmbedder()
    service = KnowledgeService(vector_store=store, embedder=embedder)

    assert isinstance(service, IKnowledgeProvider)

    # Ingest document
    doc_text = (
        "ReAct is a general paradigm for Agent workflows.\n"
        "Agents reason using Thought and then perform Action.\n"
        "Observation gives feedback from the external world."
    )
    req = IngestDocumentRequest(
        content=doc_text,
        source="agent_guide.md",
        document_id="doc-agent-1",
        chunk_size=100,
        chunk_overlap=20,
    )
    res = service.ingest_document(req)
    assert res.document_id == "doc-agent-1"
    assert res.chunks_count >= 1

    # Ingest file
    sample_file = tmp_path / "fruits.txt"
    sample_file.write_text("Fresh apple and banana smoothie ingredients.", encoding="utf-8")
    res_file = service.ingest_file(sample_file)
    assert res_file.document_id == "fruits.txt"

    # Search for agent
    hits = service.search("How do Agent workflows work?", top_k=2)
    assert len(hits) >= 1
    assert isinstance(hits[0], KnowledgeHit)
    assert "agent_guide.md" in hits[0].source
    assert hits[0].chunk_index is not None

    # Connect to knowledge tool
    tools = build_knowledge_tools(service)
    assert len(tools) == 1
    tool_res = tools[0].call({"query": "Tell me about ReAct agent", "top_k": 2}, None)
    assert tool_res.ok is True
    assert tool_res.data["count"] >= 1
    assert "<untrusted-knowledge" in tool_res.data["hits"][0]["content"]


def test_api_embedder_missing_key_raises():
    embedder = ApiEmbedder(api_key="", base_url="http://localhost:8000/v1")
    with pytest.raises(Exception) as exc_info:
        embedder.embed_texts(["hello"])
    assert "缺少 Embedding API Key" in str(exc_info.value)
