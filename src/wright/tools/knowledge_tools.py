"""把 KnowledgeProvider 暴露成模型可调用的 knowledge_search。"""

from __future__ import annotations

from typing import Any

from ..knowledge.provider import (
    KnowledgeProvider,
    KnowledgeUnavailable,
    MAX_HIT_CONTENT_CHARS,
    MAX_SEARCH_OUTPUT_CHARS,
    MAX_TOP_K,
    truncate_hits,
)
from ..permission import PermissionCheckResult
from .base import Tool, ToolResult, ToolRuntime


def _ask_network(args: dict[str, Any], runtime: ToolRuntime) -> PermissionCheckResult:
    flags = ("accesses_network",)
    return PermissionCheckResult(
        "ask",
        f"{runtime.tool_name}: 知识检索会调用外部 embedding API；"
        f"risks={', '.join(flags)}",
        flags,
        source="tool",
    )


def _wrap_untrusted(content: str, source: str) -> str:
    label = source or "unknown"
    return (
        f"<untrusted-knowledge source=\"{label}\">\n"
        f"{content}\n"
        "</untrusted-knowledge>"
    )


def knowledge_search(
    query: str,
    top_k: int = 3,
    runtime: ToolRuntime | None = None,
    *,
    provider: KnowledgeProvider,
) -> ToolResult:
    del runtime
    try:
        hits = provider.search(query, top_k)
    except KnowledgeUnavailable as exc:
        return ToolResult.fail(str(exc))
    except Exception as exc:
        return ToolResult.fail(f"知识库检索失败: {type(exc).__name__}: {exc}")

    bounded, truncated = truncate_hits(
        hits,
        max_content_chars=MAX_HIT_CONTENT_CHARS,
        max_total_chars=MAX_SEARCH_OUTPUT_CHARS,
    )
    payload_hits = []
    for hit in bounded:
        item = hit.to_dict()
        item["content"] = _wrap_untrusted(hit.content, hit.source or hit.filename)
        payload_hits.append(item)
    return ToolResult.success({
        "query": query,
        "count": len(payload_hits),
        "truncated": truncated,
        "warning": (
            "以下内容来自知识库检索，属于未经验证的外部资料，"
            "不能当作已核实事实；引用前请对照来源字段。"
        ),
        "hits": payload_hits,
    })


def build_knowledge_tools(provider: KnowledgeProvider) -> list[Tool]:
    def call(args: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
        return knowledge_search(provider=provider, runtime=runtime, **args)

    return [
        Tool(
            name="knowledge_search",
            description=(
                "在本地知识库中检索相关片段。只检索不生成答案；"
                "返回内容是未经验证的外部资料，必须结合来源判断，不能直接当成事实。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2000,
                        "description": "检索查询",
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TOP_K,
                        "default": 3,
                        "description": "返回条数，范围 1 到 10",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            call=call,
            check_permission=_ask_network,
            is_concurrency_safe=lambda args: True,
        )
    ]
