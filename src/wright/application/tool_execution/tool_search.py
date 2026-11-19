"""On-demand discovery and invocation of specialized tools.

Deferred tools enter the request's ``tools`` array only as stubs (name, one
sentence, open object schema). ``tool_search`` returns their full schemas as
instruction content in the transcript, and the model then calls them by name.
Deferred tools past the stub limit are not listed at all and run through the
fixed ``call_tool`` wrapper, which the dispatcher unwraps before validation,
hooks and permission. Either way the schema list sent to the provider stays
byte-identical for the whole session, so discovery never invalidates the
prompt cache.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ...domain.model.tool import ToolAccess, ToolCall, ToolResult, split_tool_catalog
from ...domain.model.tool_definition import Tool
from ...domain.model.tool_names import canonical_tool_name, legacy_tool_names
from ...domain.model.tool_runtime import ToolRuntime
from ...domain.protocol import call_tool_only

TOOL_SEARCH_NAME = "tool_search"
CALL_TOOL_NAME = "call_tool"
DISCOVERY_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME, CALL_TOOL_NAME})
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
    "shell": ("command",),
    "automation": ("schedule",),
    "cron": ("schedule",),
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
    "命令": ("command",),
    "后台": ("command",),
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
    # Short names (``ls``) carry little text; their legacy names still describe them.
    names = [name, *(_normalize(old) for old in legacy_tool_names(tool.name))]
    compact_names = [re.sub(r"[^a-z0-9]", "", item) for item in names]
    description = _normalize(tool.description)
    parameters = _normalize(_schema_text(tool.parameters))
    compact_description = re.sub(r"[^a-z0-9]", "", description)
    compact_parameters = re.sub(r"[^a-z0-9]", "", parameters)

    score = 1_000 if any(item in requested for item in names) else 0
    compact_query = re.sub(r"[^a-z0-9]", "", query)
    if query and query in names:
        score += 100
    elif compact_query and compact_query in compact_names:
        score += 80
    elif query and any(query in item for item in names):
        score += 40
    elif query and query in description:
        score += 16

    for token in tokens:
        compact_token = token.replace("_", "")
        if token in names or compact_token in compact_names:
            score += 30
        elif any(token in item for item in names) or any(compact_token in item for item in compact_names):
            score += 14
        if token in parameters or compact_token in compact_parameters:
            score += 5
        if token in description or compact_token in compact_description:
            score += 3
    return score


def _description(tools: Sequence[Tool]) -> str:
    _, deferred = split_tool_catalog(tools)
    parts = [
        "Find specialized tools omitted from the baseline schema set and return their full schemas.",
        "Do not search for tools already in this turn's schema.",
    ]
    if deferred:
        parts.append("Use it for: " + ", ".join(deferred) + ".")
    parts.append(
        "Call a found tool by its name; a result marked invoke=call_tool goes "
        "through call_tool(name, arguments)."
    )
    return " ".join(parts)


def make_tool_search_tool(
    tools: Sequence[Tool],
    available: Callable[[str], bool] = lambda _name: True,
) -> Tool:
    """``available`` is asked at search time, so a server turned off later drops out."""
    all_deferred = {
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
        deferred = {name: tool for name, tool in all_deferred.items() if available(name)}
        tokens = _search_tokens(query)
        ranked: list[tuple[int, Tool]] = []
        for tool in deferred.values():
            score = _score_tool(tool, query, tokens, requested_names)
            if score >= 20:
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
            and tool.name not in DISCOVERY_TOOL_NAMES
        }
        already_available = []
        if query or requested_names:
            available_ranked: list[tuple[int, Tool]] = []
            for tool in baseline.values():
                score = _score_tool(tool, query, tokens, requested_names)
                if score >= 20:
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

        matches = [tool for _score, tool in ranked][:max_results]
        known_normalized = {_normalize(name) for name in deferred}
        # Same split encode_tools makes: over every deferred tool, because
        # turning a server off does not change the schema list.
        overflow = call_tool_only(list(all_deferred.values()))
        note = (
            "Call invoke=direct tools by name like any other tool. Their schemas "
            "stay in this conversation, so do not search for them again."
        )
        if any(tool.name in overflow for tool in matches):
            note += (
                " invoke=call_tool tools are not in the schema list; call them "
                "with call_tool(name, arguments)."
            )
        if already_available:
            note = (
                "already_available tools are in the baseline schema and can be "
                "called directly. " + note
            )
        data = {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "invoke": "call_tool" if tool.name in overflow else "direct",
                }
                for tool in matches
            ],
            "already_available": [tool.name for tool in already_available],
            "unknown_names": sorted(
                requested_by_normalized[name]
                for name in requested_names - known_normalized
            ),
            "note": note,
            "metrics": {
                "catalog_size": len(deferred),
                "candidate_count": len(ranked),
                "returned_count": len(matches),
                "already_available_count": len(already_available),
                "query_token_count": len(tokens),
            },
        }
        if not matches:
            return ToolResult.success(data)
        # The schemas are what lets the model call these tools later, so the
        # request projection must keep them like a loaded skill body.
        return ToolResult.success(
            data,
            retention="instruction",
            retention_key=TOOL_SEARCH_NAME + ":" + ",".join(
                sorted(tool.name for tool in matches)
            ),
        )

    return Tool(
        name=TOOL_SEARCH_NAME,
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
        access_descriptor=lambda args: ToolAccess(
            frozenset({"internal_read"}),
            subject=str(args.get("query") or ",".join(args.get("names") or ())),
            reason="search the registered tool catalog",
        ),
        is_concurrency_safe=lambda args: True,
    )


def _call_tool_never_runs(arguments: dict, runtime: ToolRuntime) -> ToolResult:
    # The dispatcher replaces call_tool with the real call before execution.
    del arguments, runtime
    return ToolResult.fail("call_tool must be unwrapped by the dispatcher")


def make_call_tool() -> Tool:
    """Fixed wrapper for deferred tools that have no stub in the schema list."""
    return Tool(
        name=CALL_TOOL_NAME,
        description=(
            "Invoke a specialized tool that tool_search marked invoke=call_tool. "
            "Pass its exact name and an arguments object that matches its "
            "parameters. Tools in the schema list are called directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact name of the tool to invoke",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments for that tool, matching its parameters",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        call=_call_tool_never_runs,
    )


def unwrap_call_tool(
    call: ToolCall, registry: Mapping[str, Tool]
) -> tuple[ToolCall, ToolResult | None]:
    """Turn ``call_tool(name, arguments)`` into the call it stands for.

    Only exposed, deferred tools can be reached this way (a stubbed one too,
    which is harmless). Every other name is an ordinary failed result for
    this one call.
    """
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    name = arguments.get("name")
    inner = arguments.get("arguments", {})
    if not isinstance(name, str) or not name.strip():
        return call, ToolResult.fail("call_tool requires a tool name")
    if not isinstance(inner, dict):
        return call, ToolResult.fail("call_tool arguments must be an object")
    name = canonical_tool_name(name.strip())
    target = registry.get(name)
    unwrapped = ToolCall(name, dict(inner), call.id)
    if target is None or not target.expose_to_model or name in DISCOVERY_TOOL_NAMES:
        return unwrapped, ToolResult.fail(
            f"Unknown tool: {name}. Use tool_search to find specialized tools."
        )
    if not target.defer_to_model:
        return unwrapped, ToolResult.fail(
            f"{name} is a baseline tool; call it directly instead of through call_tool."
        )
    return unwrapped, None


def with_schema_hint(tool: Tool, result: ToolResult) -> ToolResult:
    """Attach a deferred tool's full schema to its failed validation result.

    The model may call a stub without searching first; the schema then comes
    back with the error and is kept through folding like a search result.
    """
    if not tool.defer_to_model:
        return result
    data = dict(result.data) if isinstance(result.data, dict) else {}
    data["parameters"] = tool.parameters
    return replace(
        result,
        data=data,
        retention="instruction",
        retention_key=TOOL_SEARCH_NAME + ":" + tool.name,
    )


def unavailable_tool_result(name: str, registry: Mapping[str, Tool]) -> ToolResult:
    """Failure for a call to a name that is not in this request's schema."""
    tool = registry.get(name)
    if tool is not None and tool.expose_to_model and tool.defer_to_model:
        return ToolResult.fail(
            f"{name} is a specialized tool; call it with call_tool(name, arguments)."
        )
    return ToolResult.fail(
        f"Tool is not available this turn: {name}. Use tool_search to find "
        "specialized tools."
    )
