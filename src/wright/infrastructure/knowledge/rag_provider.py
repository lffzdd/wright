"""可选的 RAG 检索适配器：把外部 RAGChain 适配成 KnowledgeProvider。

硬约束：import wright 不能触发 RAG 导入、模型加载或网络请求。
RAGChain 构造和 load_index 推迟到第一次 search()。
RAG 实现不在本仓库内；通过 knowledge.rag_dir / knowledge.index 设置接入
（环境变量 WRIGHT_RAG_DIR / WRIGHT_KNOWLEDGE_INDEX 仍可覆盖）。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from dotenv import dotenv_values

from ...domain.model.knowledge import (
    KnowledgeHit,
    KnowledgeUnavailable,
    knowledge_hit_from_search_result,
)
from ...domain.model.settings import KnowledgeSettings


def _setting_path(raw: str) -> Path | None:
    text = raw.strip()
    return Path(text).expanduser().resolve() if text else None


def knowledge_index_path(rag_dir: Path | None, index: str = "") -> Path:
    override = _setting_path(index)
    if override is not None:
        return override
    if rag_dir is not None:
        return rag_dir / "simple_index.json"
    return Path("simple_index.json").resolve()


class RagKnowledgeProvider:
    """懒加载、失败缓存的 RAG 检索适配器。"""

    def __init__(
        self,
        *,
        index_path: Path | None = None,
        retriever_type: str | None = None,
        use_reranker: bool | None = None,
        api_key: str | None = None,
        rag_dir: Path | None = None,
    ) -> None:
        self._rag_dir: Path | None = (
            Path(rag_dir).expanduser().resolve() if rag_dir is not None else None
        )
        self.index_path = (
            Path(index_path).expanduser().resolve()
            if index_path is not None
            else knowledge_index_path(self._rag_dir)
        )
        self.retriever_type = retriever_type or "dense"
        self.use_reranker = bool(use_reranker)
        self._api_key = api_key
        self._lock = threading.RLock()
        self._chain: object | None = None
        self._init_error: str | None = None
        self._init_attempts = 0
        self._dotenv_cache: dict[str, str] | None = None

    @classmethod
    def from_settings(cls, knowledge: KnowledgeSettings) -> RagKnowledgeProvider:
        rag_dir = _setting_path(knowledge.rag_dir)
        return cls(
            index_path=knowledge_index_path(rag_dir, knowledge.index),
            retriever_type=knowledge.retriever,
            use_reranker=knowledge.reranker,
            rag_dir=rag_dir,
        )

    def search(self, query: str, top_k: int) -> list[KnowledgeHit]:
        error = self._ensure_ready()
        if error is not None:
            raise KnowledgeUnavailable(error)
        chain = self._chain
        assert chain is not None
        try:
            raw_results = self._retrieve(chain, query, top_k)
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            # 查询期网络抖动不永久禁用；只有初始化失败才缓存。
            raise KnowledgeUnavailable(
                f"Knowledge search failed: {type(exc).__name__}: {exc}"
            ) from exc
        return [knowledge_hit_from_search_result(item) for item in raw_results]

    def _ensure_ready(self) -> str | None:
        with self._lock:
            if self._init_error is not None:
                return self._init_error
            if self._chain is not None:
                return None
            self._init_attempts += 1
            try:
                self._chain = self._initialize()
            except KnowledgeUnavailable as exc:
                self._init_error = str(exc)
                return self._init_error
            except Exception as exc:
                self._init_error = (
                    f"Knowledge base initialization failed: {type(exc).__name__}: {exc}"
                )
                return self._init_error
            return None

    def _initialize(self) -> object:
        api_key = self._resolved_api_key()
        if not api_key:
            raise KnowledgeUnavailable(
                "Missing SILICONFLOW_API_KEY. knowledge_search uses the SiliconFlow "
                "embedding API. Set that variable and start a new session, "
                "or set LLM_API_KEY as a fallback."
            )
        if not self.index_path.is_file():
            raise KnowledgeUnavailable(
                f"Knowledge index does not exist: {self.index_path}. "
                "Build the index in the RAG project, or point knowledge.index "
                "(WRIGHT_KNOWLEDGE_INDEX) at an existing simple_index.json."
            )
        rag_chain_cls = self._import_rag_chain()
        chain = rag_chain_cls(
            embedder_type="api",
            store_type="simple",
            retriever_type=self.retriever_type,
            use_reranker=self.use_reranker,
            query_rewrite="none",
            embedding_api_key=api_key,
            # knowledge_search 只使用检索器，不调用 RAGChain 的生成 LLM。
            # 仍给 OpenAI client 一个有效凭据，避免无关的构造期失败。
            llm_api_key=self._resolved_llm_api_key() or api_key,
            reranker_api_key=api_key,
        )
        loaded = chain.load_index(self.index_path)
        if not loaded:
            raise KnowledgeUnavailable(
                f"Could not load the knowledge index: {self.index_path}. "
                "The file may be corrupt or empty. Rebuild the index."
            )
        return chain

    def _resolved_api_key(self) -> str:
        if self._api_key is not None:
            return self._api_key.strip()
        return (
            os.environ.get("SILICONFLOW_API_KEY", "").strip()
            or self._dotenv_value("SILICONFLOW_API_KEY")
            or os.environ.get("LLM_API_KEY", "").strip()
            or self._dotenv_value("LLM_API_KEY")
        )

    def _resolved_llm_api_key(self) -> str:
        return (
            os.environ.get("LLM_API_KEY", "").strip()
            or self._dotenv_value("LLM_API_KEY")
        )

    def _dotenv_value(self, name: str) -> str:
        """只读 RAG 自己的 .env，不修改进程环境，也不暴露其他配置。"""
        if self._rag_dir is None:
            return ""
        if self._dotenv_cache is None:
            env_path = self._rag_dir / ".env"
            try:
                values = dotenv_values(env_path) if env_path.is_file() else {}
            except (OSError, ValueError):
                values = {}
            self._dotenv_cache = {
                str(key): str(value).strip()
                for key, value in values.items()
                if value is not None
            }
        return self._dotenv_cache.get(name, "")

    def _import_rag_chain(self):
        if self._rag_dir is None:
            raise KnowledgeUnavailable(
                "Could not import the RAG module. Set knowledge.rag_dir (WRIGHT_RAG_DIR) "
                "to a directory that contains rag_chain.py before enabling knowledge_search."
            )
        rag_dir = str(self._rag_dir)
        if rag_dir not in sys.path:
            sys.path.insert(0, rag_dir)
        try:
            from rag_chain import RAGChain
        except Exception as exc:
            raise KnowledgeUnavailable(
                "Could not import the RAG module. Check that knowledge.rag_dir points "
                f"at a directory containing rag_chain.py and that its dependencies are installed. Original error: {type(exc).__name__}: {exc}"
            ) from exc
        return RAGChain

    def _retrieve(self, chain: object, query: str, top_k: int) -> list[object]:
        hybrid = getattr(chain, "hybrid", None)
        if hybrid is not None:
            results = hybrid.search(query, top_k=top_k)
        else:
            dense = getattr(chain, "dense_retriever", None)
            if dense is None:
                raise KnowledgeUnavailable("RAGChain has no retriever")
            results = dense.search(query, top_k=top_k)
        reranker = getattr(chain, "reranker", None)
        if reranker is not None:
            results = reranker.rerank(query=query, results=results, top_n=top_k)
        if not isinstance(results, list):
            return []
        return results


__all__ = [
    "RagKnowledgeProvider",
    "knowledge_index_path",
]
