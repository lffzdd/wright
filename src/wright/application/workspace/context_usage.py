"""Split one assembled request into mutually exclusive token estimates.

The numbers use the same character estimator as context assembly. They are
not provider billing usage. A category owns each request message exactly once.
"""

from __future__ import annotations

from typing import Any

from ...utils.token_counter import estimate_message_tokens

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


__all__ = ["classify_context", "empty_context"]
