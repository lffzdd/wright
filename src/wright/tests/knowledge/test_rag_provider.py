import json
import os
import sys
from types import SimpleNamespace

import pytest

from ...knowledge.provider import KnowledgeUnavailable
from ...knowledge.rag_provider import (
    RagKnowledgeProvider,
    knowledge_enabled,
)
from ...tools.knowledge_tools import build_knowledge_tools


def test_constructing_provider_does_not_import_rag(monkeypatch, tmp_path):
    calls = {"n": 0}

    def boom(self):
        calls["n"] += 1
        raise AssertionError("不应在构造时导入 RAG")

    monkeypatch.setattr(RagKnowledgeProvider, "_import_rag_chain", boom)
    RagKnowledgeProvider(index_path=tmp_path / "missing.json", api_key="k")
    assert calls["n"] == 0
    assert "rag_chain" not in sys.modules


def test_missing_index_returns_actionable_failure(tmp_path):
    provider = RagKnowledgeProvider(
        index_path=tmp_path / "no-index.json", api_key="k"
    )
    tools = build_knowledge_tools(provider)
    result = tools[0].call({"query": "什么是 Transformer"}, None)
    assert result.ok is False
    assert "索引不存在" in result.err
    assert "WRIGHT_KNOWLEDGE_INDEX" in result.err


def test_missing_api_key_returns_actionable_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    index = tmp_path / "simple_index.json"
    index.write_text("{}", encoding="utf-8")
    provider = RagKnowledgeProvider(index_path=index, api_key="")
    result = build_knowledge_tools(provider)[0].call({"query": "q"}, None)
    assert result.ok is False
    assert "SILICONFLOW_API_KEY" in result.err


def test_import_failure_returns_actionable_failure(tmp_path, monkeypatch):
    index = tmp_path / "simple_index.json"
    index.write_text("{}", encoding="utf-8")

    def boom(self):
        raise KnowledgeUnavailable(
            "无法导入 RAG 模块。设置 WRIGHT_RAG_DIR 指向含 rag_chain.py "
            "的目录后再启用 knowledge_search。"
        )

    monkeypatch.setattr(RagKnowledgeProvider, "_import_rag_chain", boom)
    provider = RagKnowledgeProvider(index_path=index, api_key="k")
    result = build_knowledge_tools(provider)[0].call({"query": "q"}, None)
    assert result.ok is False
    assert "无法导入 RAG 模块" in result.err


def test_init_failure_is_cached(tmp_path):
    provider = RagKnowledgeProvider(
        index_path=tmp_path / "missing.json", api_key="k"
    )
    with pytest.raises(KnowledgeUnavailable, match="索引不存在"):
        provider.search("q", 3)
    assert provider._init_attempts == 1
    with pytest.raises(KnowledgeUnavailable, match="索引不存在"):
        provider.search("q", 3)
    assert provider._init_attempts == 1


def test_explicit_credentials_are_forwarded_to_rag_chain(tmp_path, monkeypatch):
    index = tmp_path / "simple_index.json"
    index.write_text("{}", encoding="utf-8")
    captured = {}

    class RecordingChain:
        def __init__(self, **kwargs):
            # 只保存匹配结果，不把凭据值留给 pytest 的失败差异输出。
            captured.update({
                "embedding_matches": (
                    kwargs.get("embedding_api_key") == "explicit-key"
                ),
                "reranker_matches": (
                    kwargs.get("reranker_api_key") == "explicit-key"
                ),
                "llm_matches": kwargs.get("llm_api_key") == "test-llm-key",
            })

        def load_index(self, path):
            return path == index

    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    provider = RagKnowledgeProvider(index_path=index, api_key="explicit-key")
    monkeypatch.setattr(provider, "_import_rag_chain", lambda: RecordingChain)

    assert provider._ensure_ready() is None
    assert captured == {
        "embedding_matches": True,
        "reranker_matches": True,
        "llm_matches": True,
    }


def test_rag_dotenv_credentials_work_without_export(tmp_path, monkeypatch):
    rag_dir = tmp_path / "rag"
    rag_dir.mkdir()
    (rag_dir / ".env").write_text(
        "SILICONFLOW_API_KEY=dotenv-embedding-key\n"
        "LLM_API_KEY=dotenv-llm-key\n",
        encoding="utf-8",
    )
    index = tmp_path / "simple_index.json"
    index.write_text("{}", encoding="utf-8")
    captured = {}

    class RecordingChain:
        def __init__(self, **kwargs):
            # 同上：断言失败时只显示布尔值，不显示任何凭据。
            captured.update({
                "embedding_matches": (
                    kwargs.get("embedding_api_key") == "dotenv-embedding-key"
                ),
                "reranker_matches": (
                    kwargs.get("reranker_api_key") == "dotenv-embedding-key"
                ),
                "llm_matches": (
                    kwargs.get("llm_api_key") == "exported-llm-key"
                ),
            })

        def load_index(self, path):
            return path == index

    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "exported-llm-key")
    provider = RagKnowledgeProvider(index_path=index, rag_dir=rag_dir)
    monkeypatch.setattr(provider, "_import_rag_chain", lambda: RecordingChain)

    assert provider._ensure_ready() is None
    assert captured == {
        "embedding_matches": True,
        "reranker_matches": True,
        "llm_matches": True,
    }


def test_real_rag_chain_loads_index_and_returns_hits_offline(tmp_path):
    """使用真实 RAGChain / SimpleVectorStore，只替换会联网的 query embedder。"""
    index = tmp_path / "simple_index.json"
    index.write_text(json.dumps({
        "vectors": [[1.0, 0.0], [0.0, 1.0]],
        "chunks": [
            {
                "content": "ReAct 循环包含 Thought、Action 和 Observation。",
                "metadata": {"source": "agent.md", "chunk_index": 0},
            },
            {
                "content": "向量检索根据查询与文档的相似度排序。",
                "metadata": {"source": "rag.md", "chunk_index": 0},
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")
    provider = RagKnowledgeProvider(
        index_path=index,
        api_key="offline-test-key",
        retriever_type="dense",
        use_reranker=False,
    )

    error = provider._ensure_ready()
    if error is not None and "无法导入 RAG 模块" in error:
        pytest.skip("optional RAG package is not on WRIGHT_RAG_DIR")
    assert error is None
    chain = provider._chain
    assert chain is not None
    assert len(chain.store) == 2
    chain.dense_retriever.embedder = SimpleNamespace(
        embed_query=lambda query: [1.0, 0.0]
    )

    hits = provider.search("ReAct 是什么", 1)
    assert len(hits) == 1
    assert hits[0].source == "agent.md"
    assert "Observation" in hits[0].content


def test_live_rag_provider_search_when_enabled():
    """显式 opt-in 的真实网络冒烟测试，不在普通回归中消耗 API 额度。"""
    if os.environ.get("WRIGHT_KNOWLEDGE_LIVE_TEST") != "1":
        pytest.skip("set WRIGHT_KNOWLEDGE_LIVE_TEST=1 to run the live RAG check")
    provider = RagKnowledgeProvider.from_env()
    result = build_knowledge_tools(provider)[0].call(
        {"query": "AI Agent 的 ReAct 工作流程是什么？", "top_k": 2},
        None,
    )
    assert result.ok, result.err
    assert result.data["count"] >= 1
    assert any(item["source"] for item in result.data["hits"])


def test_knowledge_disabled_by_default(monkeypatch):
    monkeypatch.delenv("WRIGHT_KNOWLEDGE_ENABLED", raising=False)
    assert knowledge_enabled() is False
