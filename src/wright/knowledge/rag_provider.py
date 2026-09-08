"""可选的 RAG 检索适配器：把外部 RAGChain 适配成 KnowledgeProvider。

硬约束：import wright 不能触发 RAG 导入、模型加载或网络请求。
RAGChain 构造和 load_index 推迟到第一次 search()。
RAG 实现不在本仓库内；通过 WRIGHT_RAG_DIR / WRIGHT_KNOWLEDGE_INDEX 接入。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from dotenv import dotenv_values

from .provider import (
    KnowledgeHit,
    KnowledgeUnavailable,
    knowledge_hit_from_search_result,
)

_TRUTHY = {"1", "true", "yes", "on"}


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def knowledge_enabled() -> bool:
    """默认关闭：未显式启用时 knowledge_search 不进工具集。"""
    return os.environ.get("WRIGHT_KNOWLEDGE_ENABLED", "").strip().lower() in _TRUTHY


def knowledge_rag_dir() -> Path | None:
    """外部 RAG 包目录（含 rag_chain.py）。未设置则不尝试导入。"""
    return _env_path("WRIGHT_RAG_DIR")


def knowledge_index_path() -> Path:
    override = _env_path("WRIGHT_KNOWLEDGE_INDEX")
    if override is not None:
        return override
    rag_dir = knowledge_rag_dir()
    if rag_dir is not None:
        return rag_dir / "simple_index.json"
    return Path("simple_index.json").resolve()


def knowledge_retriever_type() -> str:
    value = os.environ.get("WRIGHT_KNOWLEDGE_RETRIEVER", "dense").strip().lower()
    if value not in {"dense", "hybrid"}:
        return "dense"
    return value


def knowledge_use_reranker() -> bool:
    return os.environ.get("WRIGHT_KNOWLEDGE_RERANKER", "").strip().lower() in _TRUTHY


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
        self.index_path = (
            Path(index_path).expanduser().resolve()
            if index_path is not None
            else knowledge_index_path()
        )
        self.retriever_type = retriever_type or knowledge_retriever_type()
        self.use_reranker = (
            knowledge_use_reranker() if use_reranker is None else use_reranker
        )
        self._api_key = api_key
        if rag_dir is not None:
            self._rag_dir: Path | None = Path(rag_dir).expanduser().resolve()
        else:
            self._rag_dir = knowledge_rag_dir()
        self._lock = threading.RLock()
        self._chain: object | None = None
        self._init_error: str | None = None
        self._init_attempts = 0
        self._dotenv_cache: dict[str, str] | None = None

    @classmethod
    def from_env(cls) -> RagKnowledgeProvider:
        return cls()

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
                f"知识库检索失败: {type(exc).__name__}: {exc}"
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
                    f"知识库初始化失败: {type(exc).__name__}: {exc}"
                )
                return self._init_error
            return None

    def _initialize(self) -> object:
        api_key = self._resolved_api_key()
        if not api_key:
            raise KnowledgeUnavailable(
                "缺少 SILICONFLOW_API_KEY。knowledge_search 使用 SiliconFlow "
                "embedding API，请设置该环境变量后重开会话；"
                "也可以设置 LLM_API_KEY 作为后备。"
            )
        if not self.index_path.is_file():
            raise KnowledgeUnavailable(
                f"知识库索引不存在: {self.index_path}。"
                "请先在 RAG 项目中构建索引，或用 WRIGHT_KNOWLEDGE_INDEX "
                "指向已有的 simple_index.json。"
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
                f"无法加载知识库索引: {self.index_path}。"
                "文件可能损坏或为空，请重新构建索引。"
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
                "无法导入 RAG 模块。设置 WRIGHT_RAG_DIR 指向含 rag_chain.py "
                "的目录后再启用 knowledge_search。"
            )
        rag_dir = str(self._rag_dir)
        if rag_dir not in sys.path:
            sys.path.insert(0, rag_dir)
        try:
            from rag_chain import RAGChain
        except Exception as exc:
            raise KnowledgeUnavailable(
                "无法导入 RAG 模块。确认 WRIGHT_RAG_DIR 指向含 rag_chain.py "
                f"的目录且依赖已安装，原始错误: {type(exc).__name__}: {exc}"
            ) from exc
        return RAGChain

    def _retrieve(self, chain: object, query: str, top_k: int) -> list[object]:
        hybrid = getattr(chain, "hybrid", None)
        if hybrid is not None:
            results = hybrid.search(query, top_k=top_k)
        else:
            dense = getattr(chain, "dense_retriever", None)
            if dense is None:
                raise KnowledgeUnavailable("RAGChain 没有可用的检索器")
            results = dense.search(query, top_k=top_k)
        reranker = getattr(chain, "reranker", None)
        if reranker is not None:
            results = reranker.rerank(query=query, results=results, top_n=top_k)
        if not isinstance(results, list):
            return []
        return results
