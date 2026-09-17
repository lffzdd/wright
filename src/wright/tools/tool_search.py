"""On-demand discovery for specialized tools."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from .base import Tool, ToolResult, ToolRuntime, split_tool_catalog

MAX_ACTIVE_DEFERRED_TOOLS = 12
_STOP_WORDS = frozenset({
    "a",
    "an",
    "and",
    "another",
    "for",
    "from",
    "in",
    "new",
    "of",
    "on",
    "or",
    "the",
    "to",
    "use",
    "using",
    "with",
})
_TOKEN_ALIASES = {
    "delegate": ("spawn",),
    "delegation": ("spawn",),
    "internet": ("web", "http"),
    "recall": ("memory",),
    "remember": ("memory",),
    "recurring": ("schedule",),
}
_QUERY_ALIASES = {
    "文件": ("file",),
    "读取": ("read",),
    "读文件": ("read", "file"),
    "写入": ("write",),
    "写文件": ("write", "file"),
    "新建": ("create",),
    "网络": ("web", "http"),
    "网页": ("web",),
    "请求": ("request",),
    "搜索": ("search",),
    "计划": ("plan",),
    "任务": ("task",),
    "子代理": ("subagent",),
    "代理": ("agent",),
    "委派": ("spawn", "subagent"),
    "记忆": ("memory",),
    "历史": ("episode", "history"),
    "定时": ("schedule",),
    "周期": ("recurring", "schedule"),
    "循环": ("loop",),
    "技能": ("skill",),
    "工具": ("tool",),
}


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _search_tokens(query: str) -> set[str]:
    normalized = _normalize(query)
    tokens = {
        token
        for token in re.findall(r"[a-z0-9_]+", normalized)
        if len(token.replace("_", "")) > 1 and token not in _STOP_WORDS
    }
    for token in tuple(tokens):
        tokens.update(_TOKEN_ALIASES.get(token, ()))
    for phrase, aliases in _QUERY_ALIASES.items():
        if phrase in normalized:
            tokens.update(aliases)
    return tokens


def _schema_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(
            [*(str(key) for key in value), *(_schema_text(item) for item in value.values())]
        )
    if isinstance(value, list):
        return " ".join(_schema_text(item) for item in value)
    return str(value) if isinstance(value, (str, int, float, bool)) else ""


def _score_tool(tool: Tool, query: str, tokens: set[str], requested: set[str]) -> int:
    name = _normalize(tool.name)
    description = _normalize(tool.description)
    parameters = _normalize(_schema_text(tool.parameters))
    compact_name = re.sub(r"[^a-z0-9]", "", name)
    compact_description = re.sub(r"[^a-z0-9]", "", description)
    compact_parameters = re.sub(r"[^a-z0-9]", "", parameters)

    score = 1_000 if name in requested else 0
    compact_query = re.sub(r"[^a-z0-9]", "", query)
    if query and query == name:
        score += 100
    elif compact_query and compact_query == compact_name:
        score += 80
    elif query and query in name:
        score += 40
    elif query and query in description:
        score += 16

    for token in tokens:
        compact_token = token.replace("_", "")
        if token == name or compact_token == compact_name:
            score += 30
        elif token in name or compact_token in compact_name:
            score += 14
        if token in parameters or compact_token in compact_parameters:
            score += 5
        if token in description or compact_token in compact_description:
            score += 3
    return score


def _description(tools: Sequence[Tool]) -> str:
    _, deferred = split_tool_catalog(tools)
    parts = [
        "Find and activate specialized tools omitted from the baseline schema set.",
        "Do not search for tools already in this turn's schema.",
    ]
    if deferred:
        parts.append("Use it for: " + ", ".join(deferred) + ".")
    parts.append("Activated tools are available on the next model turn.")
    return " ".join(parts)


def make_tool_search_tool(tools: Sequence[Tool], active: list[str]) -> Tool:
    deferred = {
        tool.name: tool
        for tool in tools
        if tool.expose_to_model and tool.defer_to_model
    }

    def search(arguments: dict, runtime: ToolRuntime) -> ToolResult:
        runtime.raise_if_cancelled()
        query = _normalize(str(arguments.get("query") or ""))
        requested = arguments.get("names") or []
        max_results = max(1, min(int(arguments.get("max_results", 5)), 20))
        if not query and not requested:
            return ToolResult.fail("query or names is required")

        requested_by_normalized = {
            _normalize(str(name)): str(name).strip()
            for name in requested
            if str(name).strip()
        }
        requested_names = set(requested_by_normalized)
        tokens = _search_tokens(query)
        ranked: list[tuple[int, Tool]] = []
        for tool in deferred.values():
            score = _score_tool(tool, query, tokens, requested_names)
            if score >= 20 or score >= 1_000:
                ranked.append((score, tool))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        if ranked:
            relative_floor = max(20, int(ranked[0][0] * 0.3 + 0.999))
            ranked = [
                item
                for item in ranked
                if item[0] >= relative_floor or item[0] >= 1_000
            ]

        baseline = {
            tool.name: tool
            for tool in tools
            if tool.expose_to_model and not tool.defer_to_model
            and tool.name != "tool_search"
        }
        already_available = []
        if query or requested_names:
            available_ranked: list[tuple[int, Tool]] = []
            for tool in baseline.values():
                score = _score_tool(tool, query, tokens, requested_names)
                if score >= 20 or score >= 1_000:
                    available_ranked.append((score, tool))
            available_ranked.sort(key=lambda item: (-item[0], item[1].name))
            already_available = [tool for _score, tool in available_ranked[:max_results]]
            if already_available:
                baseline_floor = max(20, int(available_ranked[0][0] * 0.5 + 0.999))
                ranked = [
                    item
                    for item in ranked
                    if item[0] >= baseline_floor or item[0] >= 1_000
                ]

        active_set = set(active)
        already_active = [
            tool for _score, tool in ranked if tool.name in active_set
        ]
        matches = [
            tool for _score, tool in ranked if tool.name not in active_set
        ][:min(max_results, MAX_ACTIVE_DEFERRED_TOOLS)]
        for tool in already_active:
            active.remove(tool.name)
            active.append(tool.name)
        active.extend(tool.name for tool in matches)
        evicted: list[str] = []
        while len(active) > MAX_ACTIVE_DEFERRED_TOOLS:
            evicted.append(active.pop(0))

        known_normalized = {_normalize(name) for name in deferred}
        note = "Activated tools are available on the next model turn."
        if already_available:
            note = (
                "already_available tools are in the baseline schema and can be "
                "called this turn. " + note
            )
        if evicted:
            note += (
                " Evicted tools were dropped from the active set and must be "
                "searched again: " + ", ".join(evicted) + "."
            )
        return ToolResult.success({
            "activated": [
                {"name": tool.name, "description": tool.description}
                for tool in matches
            ],
            "already_available": [tool.name for tool in already_available],
            "already_active": [tool.name for tool in already_active],
            "evicted": evicted,
            "unknown_names": sorted(
                requested_by_normalized[name]
                for name in requested_names - known_normalized
            ),
            "note": note,
            "metrics": {
                "catalog_size": len(deferred),
                "candidate_count": len(ranked),
                "activated_count": len(matches),
                "already_available_count": len(already_available),
                "already_active_count": len(already_active),
                "evicted_count": len(evicted),
                "active_count": len(active),
                "query_token_count": len(tokens),
            },
        })

    return Tool(
        name="tool_search",
        description=_description(tools),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Capability, keyword, or exact tool name to find",
                },
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": "Optional exact tool names when already known",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
            },
            "additionalProperties": False,
        },
        call=search,
    )
