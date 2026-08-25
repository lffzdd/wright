from types import SimpleNamespace

from ...knowledge.provider import (
    KnowledgeHit,
    knowledge_hit_from_search_result,
    truncate_hits,
)


def test_search_result_field_mapping():
    result = SimpleNamespace(
        score=0.42,
        chunk=SimpleNamespace(
            content="正文",
            metadata={
                "source": "docs/a.md",
                "document_id": "doc-1",
                "filename": "a.md",
                "filepath": "/tmp/a.md",
                "chunk_index": 2,
                "chunk_total": 5,
                "page": 3,
            },
        ),
    )
    hit = knowledge_hit_from_search_result(result)
    assert hit.content == "正文"
    assert hit.score == 0.42
    assert hit.source == "docs/a.md"
    assert hit.document_id == "doc-1"
    assert hit.filename == "a.md"
    assert hit.filepath == "/tmp/a.md"
    assert hit.chunk_index == 2
    assert hit.chunk_total == 5
    assert hit.page == 3


def test_missing_metadata_does_not_raise():
    result = SimpleNamespace(score="not-a-float", chunk=None)
    hit = knowledge_hit_from_search_result(result)
    assert hit.content == ""
    assert hit.score == 0.0
    assert hit.source == ""
    assert hit.chunk_index is None
    assert hit.page is None


def test_partial_metadata_uses_defaults():
    result = SimpleNamespace(
        score=1,
        chunk=SimpleNamespace(content="x", metadata={"filename": "only.md"}),
    )
    hit = knowledge_hit_from_search_result(result)
    assert hit.filename == "only.md"
    assert hit.source == ""
    assert hit.document_id == ""


def test_content_truncation_and_total_cap():
    long_hit = KnowledgeHit(content="汉" * 3000, score=1.0, source="a")
    bounded, truncated = truncate_hits([long_hit], max_content_chars=2000)
    assert truncated is True
    assert len(bounded[0].content) == 2000

    many = [
        KnowledgeHit(content="a" * 100, score=1.0, source=str(i))
        for i in range(20)
    ]
    bounded, truncated = truncate_hits(many, max_total_chars=250)
    assert truncated is True
    assert sum(len(hit.content) for hit in bounded) <= 250
    assert 1 <= len(bounded) < 20
