"""Split one assembled request into mutually exclusive token estimates.

The numbers use the same character estimator as context assembly. They are
not provider billing usage. A category owns each request message exactly once,
except the system prompt, whose resident memory block is counted as memory.

Categories fall in three groups: ``fixed`` is sent with every request whatever
the conversation holds, ``conversation`` grows as the session goes on, and
``reserve`` is room kept free for the reply. Each category lists its largest
items so the Context tab can show what fills it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ...domain.model.mcp import mcp_server_of
from ...utils.token_counter import (
    estimate_message_tokens,
    estimate_tokens,
    estimate_tools_tokens,
)

_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("system_prompt", "fixed"),
    ("tool_schemas", "fixed"),
    ("skills", "fixed"),
    ("memory", "fixed"),
    ("user", "conversation"),
    ("assistant", "conversation"),
    ("tool_output", "conversation"),
    ("plan", "conversation"),
    ("reminders", "conversation"),
    ("output_reserve", "reserve"),
)
# Items listed per category; the rest are only counted.
MAX_ITEMS = 8
_LABEL_CHARS = 80
_ARG_KEYS = ("path", "file_path", "command", "cmd", "query", "pattern", "url", "name", "skill", "task")
_MEMORY_OPEN = "<MEMORY_CONTEXT>"
_MEMORY_CLOSE = "</MEMORY_CONTEXT>"

NOTE = (
    "Estimated with the local character tokenizer from the messages, tool "
    "schemas, and output reserve assembled for the request, after older tool "
    "output is folded. Provider billing usage is reported separately."
)
PREVIEW_NOTE = (
    "Estimated for the next request from the transcript, system prompt, "
    "reminders, and tool schemas, folded the way the request will be. Memory "
    "recall is added when the request is actually assembled. Provider billing "
    "usage is reported separately."
)


class _Tally:
    def __init__(self) -> None:
        self.tokens = 0
        self.count = 0
        self.items: list[dict[str, Any]] = []

    def add(self, tokens: int, label: str, **extra: Any) -> None:
        self.tokens += tokens
        self.count += 1
        item = {"label": _clip(label), "tokens": tokens}
        if isinstance(extra.get("detail"), str):
            extra["detail"] = _clip(extra["detail"])
        item.update({key: value for key, value in extra.items() if value not in (None, "", False)})
        self.items.append(item)


def classify_context(view: Any, *, preview: bool = False) -> dict[str, Any]:
    tallies = {name: _Tally() for name, _ in _CATEGORIES}
    contains_memory = False
    calls: dict[str, tuple[str, str]] = {}
    for entry in getattr(view, "entries", ()) or ():
        message = entry.message if isinstance(entry.message, dict) else {}
        source = getattr(entry, "source", "") or ""
        if message.get("role") == "system" and _MEMORY_OPEN in _text(message):
            contains_memory = True
        _classify(message, source, tallies, calls)
    _tool_schemas(tallies, getattr(view, "tools", ()) or (), int(getattr(view, "tool_schema_tokens", 0) or 0))
    reserve = int(getattr(view, "output_reserve_tokens", 0) or 0)
    tallies["output_reserve"].tokens = reserve
    total = sum(tally.tokens for tally in tallies.values())
    folded = [item for item in tallies["tool_output"].items if item.get("folded")]
    report = {
        "kind": "tokenizer_estimate",
        "exact": False,
        "total": total,
        # What the request itself takes; the reserve is room kept for the reply.
        "used": total - reserve,
        "limit": getattr(view, "context_limit", None),
        "system_prompt_contains_memory": contains_memory,
        "folded": {
            "count": len(folded),
            "tokens": max(0, int(getattr(view, "folded_tokens", 0) or 0)),
        },
        "categories": [_category(name, group, tallies[name], total) for name, group in _CATEGORIES],
        "note": PREVIEW_NOTE if preview else NOTE,
    }
    if preview:
        report["preview"] = True
    return report


def empty_context(limit: int | None) -> dict[str, Any]:
    return {
        "kind": "tokenizer_estimate",
        "exact": False,
        "total": 0,
        "used": 0,
        "limit": limit,
        "system_prompt_contains_memory": False,
        "folded": {"count": 0, "tokens": 0},
        "categories": [
            {"id": name, "group": group, "tokens": 0, "share": 0, "count": 0, "items": []}
            for name, group in _CATEGORIES
        ],
        "note": "No model request has been assembled for this session yet.",
    }


def _category(name: str, group: str, tally: _Tally, total: int) -> dict[str, Any]:
    items = sorted(tally.items, key=lambda item: item["tokens"], reverse=True)[:MAX_ITEMS]
    return {
        "id": name,
        "group": group,
        "tokens": tally.tokens,
        "share": (tally.tokens / total) if total else 0,
        "count": tally.count,
        "items": items,
    }


def _classify(
    message: dict[str, Any],
    source: str,
    tallies: dict[str, _Tally],
    calls: dict[str, tuple[str, str]],
) -> None:
    role = message.get("role")
    content = _text(message)
    tokens = estimate_message_tokens(message)
    if role == "system":
        memory = _memory_block(content)
        memory_tokens = min(tokens, estimate_tokens(memory)) if memory else 0
        if memory_tokens:
            tallies["memory"].add(memory_tokens, "Resident memory", key="resident_memory")
        tallies["system_prompt"].add(tokens - memory_tokens, "System prompt", key="system_prompt")
        return
    if role == "tool":
        _tool_result(message, tokens, tallies, calls)
        return
    if role == "assistant":
        names = []
        for call in message.get("tool_calls") or ():
            name, summary = _call_summary(call)
            call_id = call.get("id") if isinstance(call, Mapping) else None
            if isinstance(call_id, str) and call_id:
                calls[call_id] = (name, summary)
            names.append(name)
        label = _first_line(content) or (", ".join(names) if names else "Reasoning")
        key = "" if content or names else "reasoning"
        tallies["assistant"].add(tokens, label, key=key, detail=", ".join(names) if content and names else "")
        return
    if "wright-fact-recall" in content:
        tallies["memory"].add(tokens, "Recalled facts", key="recalled_facts")
    elif "wright-episode-recall" in content:
        tallies["memory"].add(tokens, "Recalled episodes", key="recalled_episodes")
    elif "<skill-catalog>" in content:
        tallies["skills"].add(tokens, "Skill catalog", key="skill_catalog", count=content.count("\n- "))
    elif "<plan-state>" in content:
        tallies["plan"].add(tokens, "Plan", key="plan")
    elif source == "transient":
        tallies["reminders"].add(tokens, _first_line(_strip_tags(content)) or "Reminder")
    else:
        tallies["user"].add(tokens, _first_line(content) or "Attachment")


def _tool_result(
    message: dict[str, Any],
    tokens: int,
    tallies: dict[str, _Tally],
    calls: dict[str, tuple[str, str]],
) -> None:
    call_id = message.get("tool_call_id") if isinstance(message.get("tool_call_id"), str) else ""
    name, summary = calls.get(call_id, (str(message.get("name") or "tool"), ""))
    payload = _payload(message)
    key = payload.get("retention_key") if isinstance(payload.get("retention_key"), str) else ""
    folded = bool(payload.get("folded"))
    if key.startswith("skill:"):
        # A loaded skill body stays in every request as an instruction.
        tallies["skills"].add(tokens, key.removeprefix("skill:"), loaded=True, folded=folded, call_id=call_id)
        return
    tallies["tool_output"].add(tokens, name, detail=summary, folded=folded, call_id=call_id)


def _tool_schemas(tallies: dict[str, _Tally], schemas: Any, total: int) -> None:
    tally = tallies["tool_schemas"]
    servers: dict[str, list[int]] = {}
    for schema in schemas:
        if not isinstance(schema, Mapping):
            continue
        name = _schema_name(schema)
        tokens = estimate_tools_tokens([dict(schema)])
        server = mcp_server_of(name)
        if server is not None:
            servers.setdefault(server, []).append(tokens)
        else:
            tally.add(tokens, name)
    for server, sizes in servers.items():
        tally.add(sum(sizes), server, mcp=server, count=len(sizes))
    # The whole-list estimate is what the request budget uses; items are per tool.
    tally.tokens = total


def _schema_name(schema: Mapping[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "")
    return str(schema.get("name") or "")


def _call_summary(call: Any) -> tuple[str, str]:
    if not isinstance(call, Mapping):
        return "tool", ""
    function = call.get("function") if isinstance(call.get("function"), Mapping) else call
    name = str(function.get("name") or "tool")
    arguments = function.get("arguments", function.get("args"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return name, arguments
    if isinstance(arguments, Mapping):
        for key in _ARG_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return name, _first_line(value)
        for value in arguments.values():
            if isinstance(value, str) and value.strip():
                return name, _first_line(value)
    return name, ""


def _payload(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, str):
        return {}
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _memory_block(content: str) -> str:
    start = content.find(_MEMORY_OPEN)
    end = content.find(_MEMORY_CLOSE, start)
    if start < 0 or end < 0:
        return ""
    return content[start:end + len(_MEMORY_CLOSE)]


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content if isinstance(part, Mapping)
        )
    return ""


def _strip_tags(text: str) -> str:
    lines = [line for line in text.splitlines() if not (line.startswith("<") and line.endswith(">"))]
    return "\n".join(lines)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _clip(text: str) -> str:
    return text if len(text) <= _LABEL_CHARS else text[: _LABEL_CHARS - 1] + "…"


__all__ = ["MAX_ITEMS", "classify_context", "empty_context"]
