"""Split one assembled request into mutually exclusive token estimates.

The numbers use the same character estimator as context assembly. They are
not provider billing usage. A category owns each request message exactly once.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ...utils.token_counter import estimate_message_tokens, estimate_tools_tokens

_CATEGORIES = (
    "system_prompt",
    "memory",
    "rules",
    "plan",
    "history",
    "tool_output",
    "instructions",
    "tool_schemas",
    "output_reserve",
)


def classify_context(view: Any) -> dict[str, Any]:
    totals = dict.fromkeys(_CATEGORIES, 0)
    contains_core = False
    for entry in getattr(view, "entries", ()) or ():
        message = entry.message
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, str) and "<CORE_MEMORY>" in content and message.get("role") == "system":
            contains_core = True
        bucket = _bucket(entry)
        totals[bucket] += estimate_message_tokens(message if isinstance(message, dict) else {})
    totals["tool_schemas"] = int(getattr(view, "tool_schema_tokens", 0) or 0)
    totals["output_reserve"] = int(getattr(view, "output_reserve_tokens", 0) or 0)
    total = sum(totals.values())
    limit = getattr(view, "context_limit", None)
    return {
        "kind": "tokenizer_estimate",
        "exact": False,
        "total": total,
        "limit": limit,
        "system_prompt_contains_core_memory": contains_core,
        "categories": [
            {
                "id": name,
                "tokens": totals[name],
                "share": (totals[name] / total) if total else 0,
            }
            for name in _CATEGORIES
        ],
        "note": (
            "Estimated with the local character tokenizer from the messages, "
            "tool schemas, and output reserve actually assembled for the request. "
            "Provider billing usage is reported separately."
        ),
    }


def estimate_context(
    records: Any,
    *,
    tools: Any,
    system_prompt: str | None,
    context_limit: int | None,
) -> dict[str, Any]:
    """Estimate the next request before one has been assembled.

    It reads the transcript, the current system prompt and the visible tool
    schemas without running compaction or memory recall, so it has no side
    effects. The real breakdown replaces it once a request is built.
    """
    entries = []
    has_system = False
    for record in records or ():
        message = dict(getattr(record, "message", {}) or {})
        if message.get("role") == "system":
            has_system = True
            if system_prompt is not None:
                message["content"] = system_prompt
        entries.append(SimpleNamespace(message=message, source=getattr(record, "source", "")))
    if system_prompt is not None and not has_system:
        entries.insert(0, SimpleNamespace(message={"role": "system", "content": system_prompt}, source="transient"))
    reserve = max(256, context_limit // 8) if context_limit is not None else 0
    view = SimpleNamespace(
        entries=entries,
        tool_schema_tokens=estimate_tools_tokens(list(tools or ())),
        output_reserve_tokens=reserve,
        context_limit=context_limit,
    )
    result = classify_context(view)
    result["preview"] = True
    result["note"] = (
        "Estimated before the next request from the transcript, system prompt, "
        "and tool schemas. Memory recall and compaction are applied when the "
        "request is actually assembled. Provider billing usage is reported separately."
    )
    return result


def empty_context(limit: int | None) -> dict[str, Any]:
    return {
        "kind": "tokenizer_estimate",
        "exact": False,
        "total": 0,
        "limit": limit,
        "system_prompt_contains_core_memory": False,
        "categories": [{"id": name, "tokens": 0, "share": 0} for name in _CATEGORIES],
        "note": "No model request has been assembled for this session yet.",
    }


def _bucket(entry: Any) -> str:
    message = getattr(entry, "message", {}) or {}
    role = message.get("role")
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    source = getattr(entry, "source", "") or ""
    if role == "system":
        return "system_prompt"
    if role == "tool":
        return "tool_output"
    if "wright-semantic-recall" in content or "wright-episode-recall" in content:
        return "memory"
    if "<skill-catalog>" in content:
        return "rules"
    if "<plan-state>" in content:
        return "plan"
    if source == "transient":
        return "instructions"
    return "history"


__all__ = ["classify_context", "empty_context", "estimate_context"]
