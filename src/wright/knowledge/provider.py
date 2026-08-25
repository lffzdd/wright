"""知识检索的项目内类型，刻意不依赖隔壁 RAG 的 SearchResult。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


MAX_HIT_CONTENT_CHARS = 2_000
MAX_SEARCH_OUTPUT_CHARS = 8_000
MAX_TOP_K = 10


class KnowledgeUnavailable(RuntimeError):
    """检索能力当前不可用；工具层应转成 ToolResult.fail，而不是炸掉回合。"""


@dataclass(frozen=True)
class KnowledgeHit:
    content: str
    score: float
    source: str = ""
    document_id: str = ""
    filename: str = ""
    filepath: str = ""
    chunk_index: int | None = None
    chunk_total: int | None = None
    page: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "content": self.content,
            "score": self.score,
            "source": self.source,
            "document_id": self.document_id,
            "filename": self.filename,
            "filepath": self.filepath,
            "chunk_index": self.chunk_index,
            "chunk_total": self.chunk_total,
            "page": self.page,
        }


class KnowledgeProvider(Protocol):
    def search(self, query: str, top_k: int) -> list[KnowledgeHit]:
        """返回项目内的命中列表；不可用时抛 KnowledgeUnavailable。"""


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _optional_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def knowledge_hit_from_search_result(result: object) -> KnowledgeHit:
    """把 RAG 的 SearchResult 鸭类型翻译成本项目的 KnowledgeHit。

    不 import RAG 类型：测试和降级路径都只依赖 chunk/content/metadata/score。
    metadata 缺字段时填空，而不是抛异常。
    """
    chunk = getattr(result, "chunk", None)
    raw_content = getattr(chunk, "content", "") if chunk is not None else ""
    content = raw_content if isinstance(raw_content, str) else _optional_str(raw_content)
    metadata = getattr(chunk, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}

    raw_score = getattr(result, "score", 0.0)
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        score = 0.0

    return KnowledgeHit(
        content=content,
        score=score,
        source=_optional_str(metadata.get("source")),
        document_id=_optional_str(
            metadata.get("document_id") or metadata.get("doc_id")
        ),
        filename=_optional_str(metadata.get("filename")),
        filepath=_optional_str(metadata.get("filepath")),
        chunk_index=_optional_int(metadata.get("chunk_index")),
        chunk_total=_optional_int(metadata.get("chunk_total")),
        page=_optional_int(metadata.get("page")),
    )


def truncate_hits(
    hits: list[KnowledgeHit],
    *,
    max_content_chars: int = MAX_HIT_CONTENT_CHARS,
    max_total_chars: int = MAX_SEARCH_OUTPUT_CHARS,
) -> tuple[list[KnowledgeHit], bool]:
    """截断单条正文和总输出，避免检索结果淹没上下文。"""
    truncated = False
    bounded: list[KnowledgeHit] = []
    used = 0
    for hit in hits:
        content = hit.content
        if len(content) > max_content_chars:
            content = content[:max_content_chars]
            truncated = True
        remaining = max_total_chars - used
        if remaining <= 0:
            truncated = True
            break
        if len(content) > remaining:
            content = content[:remaining]
            truncated = True
        bounded.append(KnowledgeHit(
            content=content,
            score=hit.score,
            source=hit.source,
            document_id=hit.document_id,
            filename=hit.filename,
            filepath=hit.filepath,
            chunk_index=hit.chunk_index,
            chunk_total=hit.chunk_total,
            page=hit.page,
        ))
        used += len(content)
    return bounded, truncated
