"""Split one assembled request into mutually exclusive token estimates.

The numbers use the same character estimator as context assembly. They are
not provider billing usage. A category owns each request message exactly once,
except the system prompt, whose resident memory block is counted as memory.

Categories fall in three groups: ``fixed`` is sent with every request whatever
the conversation holds, ``conversation`` grows as the session goes on, and
``reserve`` is room kept free for the reply. Each category lists its largest
items so the Context tab can show what fills it, and tool categories also sum
their items per tool.

The estimate drifts from what the provider counts. ``calibrate`` anchors it on
the provider's prompt token count for the last request, so only what was added
since is estimated.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from ...domain.model.mcp import mcp_server_of
from ...domain.model.session.conversation import USER_INPUT_SOURCE
from ...domain.prompt import system_prompt_sections
from ...utils.token_counter import (
    estimate_message_tokens,
    estimate_tokens,
    estimate_tools_tokens,
)

# In the order a request lays them out: the system prompt with resident memory
# appended, the tool list, then the conversation. The skill catalog rides at
# the tail of each request, but it is listed with what every request sends.
_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("system_prompt", "fixed"),
    ("memory", "fixed"),
    ("tool_schemas", "fixed"),
    ("mcp_schemas", "fixed"),
    ("skills", "fixed"),
    ("user", "conversation"),
    ("attachments", "conversation"),
    ("assistant", "conversation"),
    ("reasoning", "conversation"),
    ("tool_calls", "conversation"),
    ("tool_output", "conversation"),
    ("mcp_output", "conversation"),
    ("subagent_output", "conversation"),
    ("loaded_skills", "conversation"),
    ("recalled_memory", "conversation"),
    ("plan", "conversation"),
    ("reminders", "conversation"),
    ("output_reserve", "reserve"),
)
# Categories whose items are also summed per tool name.
_BY_TOOL = frozenset({"tool_calls", "tool_output", "mcp_output", "subagent_output"})
_SUBAGENT_TOOLS = frozenset({"spawn_agent", "wait_agent", "get_agent", "get_agent_tree", "cancel_agent"})
# Same flat charge per image as ``estimate_message_tokens``.
_IMAGE_TOKENS = 1024
# Requests kept for the Context tab's trend line.
HISTORY_LIMIT = 50
# Items listed per category; the rest are only counted.
MAX_ITEMS = 8
# Turns listed in the snapshot; older ones are only counted.
MAX_TURNS = 200
# Characters of content each item carries in the detail view.
CONTENT_CHARS = 64_000
# How each conversation category adds up in the per-turn view.
_TURN_PARTS = ("text", "reasoning", "tool_calls", "tool_results")
_TURN_PART_OF = {
    "user": "text",
    "attachments": "text",
    "assistant": "text",
    "reasoning": "reasoning",
    "tool_calls": "tool_calls",
    "tool_output": "tool_results",
    "mcp_output": "tool_results",
    "subagent_output": "tool_results",
    "loaded_skills": "tool_results",
}
_SYSTEM_KEYS = frozenset({"role", "tools", "rules"})
# Headed sections with a fixed label, so the UI does not show the heading
# itself, which reads like the memory content rather than how to use it.
_SYSTEM_HEADINGS = {"Long-term memory": "system_memory"}
_LABEL_CHARS = 80
_ARG_KEYS = ("path", "file_path", "command", "cmd", "query", "pattern", "url", "name", "skill", "task")
_MEMORY_OPEN = "<MEMORY_CONTEXT>"
_MEMORY_CLOSE = "</MEMORY_CONTEXT>"

NOTE = (
    "Categories are estimated with the local character tokenizer from the "
    "messages, tool schemas, and output reserve assembled for the request, "
    "after older tool output is folded. Once a request has returned, the "
    "total is anchored on the prompt tokens the provider counted for it, and "
    "only what was added since is estimated."
)
PREVIEW_NOTE = (
    "Estimated for the next request from the transcript, system prompt, "
    "reminders, and tool schemas, folded the way the request will be. Memory "
    "recall is added when the request is actually assembled. Once a request "
    "has returned, the total is anchored on the prompt tokens the provider "
    "counted for it."
)


class _Tally:
    def __init__(self, detail: bool) -> None:
        self.detail = detail
        self.tokens = 0
        self.count = 0
        self.items: list[dict[str, Any]] = []
        # tool name -> [calls, tokens]
        self.tools: dict[str, list[int]] = {}

    def add_tool(self, name: str, tokens: int) -> None:
        entry = self.tools.setdefault(name, [0, 0])
        entry[0] += 1
        entry[1] += tokens

    def add(self, tokens: int, label: str, *, content: Callable[[], str] | None = None, **extra: Any) -> dict[str, Any]:
        self.tokens += tokens
        self.count += 1
        item = {"label": _clip(label), "tokens": tokens}
        if isinstance(extra.get("detail"), str):
            extra["detail"] = _clip(extra["detail"])
        # Leave out empty values, but keep 0 (the first turn is 0).
        item.update({key: value for key, value in extra.items() if value is not None and value != "" and value is not False})
        if self.detail and content is not None:
            text = content()
            if len(text) > CONTENT_CHARS:
                item["truncated"] = True
                text = text[:CONTENT_CHARS]
            item["content"] = text
        self.items.append(item)
        return item


class _Pass:
    """State for one walk over a request: tallies, tool calls, and turns."""

    def __init__(self, detail: bool) -> None:
        self.detail = detail
        self.tallies = {name: _Tally(detail) for name, _ in _CATEGORIES}
        self.calls: dict[str, tuple[str, str]] = {}
        self.turns: list[dict[str, Any]] = []
        self.seq = 0
        # Where the item sits on the wire: the system message, then the tool
        # list, then the rest of the messages. Tools are walked last but sort
        # by this first, so ``seq`` follows the request.
        self.slot = 0
        # Set per message while it is classified.
        self.base = ""
        self.turn: int | None = None

    def add(self, category: str, tokens: int, label: str, suffix: str = "", **extra: Any) -> dict[str, Any]:
        self.seq += 1
        extra.setdefault("turn", self.turn)
        if self.detail:
            extra["seq"] = self.slot * 1_000_000 + self.seq
        return self.tallies[category].add(tokens, label, id=self.base + suffix, **extra)

    def begin_turn(self, record_id: str, label: str) -> None:
        self.turns.append({
            "index": len(self.turns),
            "label": _clip(label),
            "record_id": record_id,
            "tokens": 0,
            "parts": dict.fromkeys(_TURN_PARTS, 0),
        })
        self.turn = len(self.turns) - 1

    def summarize_turns(self) -> None:
        for name, tally in self.tallies.items():
            part = _TURN_PART_OF.get(name)
            if part is None:
                continue
            for item in tally.items:
                turn = item.get("turn")
                if isinstance(turn, int):
                    self.turns[turn]["parts"][part] += item["tokens"]
                    self.turns[turn]["tokens"] += item["tokens"]


def classify_context(
    view: Any,
    *,
    preview: bool = False,
    watermark: float | None = None,
    limit: int | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    """``watermark`` is the compactor's share of the limit where folding starts.

    ``limit`` is the model's context window; it defaults to the view's own.
    ``detail`` lists every item with its full content, for the detail view;
    otherwise each category keeps only its largest items.
    """
    walk = _Pass(detail)
    tallies = walk.tallies
    contains_memory = False
    for index, entry in enumerate(getattr(view, "entries", ()) or ()):
        message = entry.message if isinstance(entry.message, dict) else {}
        source = getattr(entry, "source", "") or ""
        record_id = getattr(entry, "record_id", None)
        walk.base = str(record_id) if record_id else f"m{index}"
        if message.get("role") == "system" and _MEMORY_OPEN in _text(message):
            contains_memory = True
        if source == "transient" or message.get("role") == "system":
            walk.turn = None
        elif message.get("role") == "user" and source == USER_INPUT_SOURCE:
            walk.begin_turn(walk.base, _first_line(_text(message)) or "Turn")
        elif walk.turn is None and walk.turns:
            walk.turn = len(walk.turns) - 1
        walk.slot = 0 if message.get("role") == "system" else 2
        _classify(message, source, walk)
    walk.slot = 1
    _tool_schemas(walk, getattr(view, "tools", ()) or (), int(getattr(view, "tool_schema_tokens", 0) or 0))
    reserve = int(getattr(view, "output_reserve_tokens", 0) or 0)
    tallies["output_reserve"].tokens = reserve
    total = sum(tally.tokens for tally in tallies.values())
    folded = [item for item in tallies["tool_output"].items if item.get("folded")]
    walk.summarize_turns()
    turns = walk.turns
    report = {
        "kind": "tokenizer_estimate",
        "exact": False,
        "total": total,
        # What the request itself takes; the reserve is room kept for the reply.
        "used": total - reserve,
        "limit": limit if limit is not None else getattr(view, "context_limit", None),
        "system_prompt_contains_memory": contains_memory,
        "folded": {
            "count": len(folded),
            "tokens": max(0, int(getattr(view, "folded_tokens", 0) or 0)),
        },
        "categories": [_category(name, group, tallies[name], total, detail) for name, group in _CATEGORIES],
        "turns": turns if detail else turns[-MAX_TURNS:],
        "note": PREVIEW_NOTE if preview else NOTE,
    }
    if not detail and len(turns) > MAX_TURNS:
        report["turns_omitted"] = len(turns) - MAX_TURNS
    limit = report["limit"]
    if watermark is not None and isinstance(limit, int) and limit > 0:
        # The compactor compares the request plus the reserve to the watermark.
        report["fold_at"] = max(0, int(limit * watermark) - reserve)
    if preview:
        report["preview"] = True
    return report


def context_detail(report: Mapping[str, Any], *, category: str | None = None, turn: int | None = None) -> dict[str, Any]:
    """Every item of one category or one turn from a ``detail`` report, in request order."""
    categories = [item for item in report.get("categories", ()) if isinstance(item, Mapping)]
    if category is not None:
        chosen = [item for item in categories if item.get("id") == category]
        if not chosen:
            raise KeyError(category)
    else:
        chosen = categories
    items = []
    for entry in chosen:
        for item in entry.get("items", ()):
            if turn is None or item.get("turn") == turn:
                items.append({**item, "category": entry.get("id")})
    if turn is not None:
        turns = [item for item in report.get("turns", ()) if item.get("index") == turn]
        if not turns:
            raise KeyError(turn)
    items.sort(key=lambda item: item.get("seq", 0))
    result: dict[str, Any] = {"items": items, "tokens": sum(item["tokens"] for item in items)}
    if category is not None:
        result["category"] = chosen[0]
    if turn is not None:
        result["turn"] = turns[0]
    return result


def history_entry(breakdown: Mapping[str, Any], prompt_tokens: int | None) -> dict[str, int] | None:
    """One trend point: the estimate for a request and what the provider counted."""
    estimated = breakdown.get("used")
    if not isinstance(estimated, int):
        return None
    entry = {"estimated": estimated}
    if isinstance(prompt_tokens, int) and prompt_tokens > 0:
        entry["actual"] = prompt_tokens
    return entry


def calibrate(breakdown: dict[str, Any], history: Any) -> dict[str, Any]:
    """Attach the trend and anchor the total on the last provider count.

    The anchor is the newest request the provider counted. What the current
    estimate adds beyond that request's estimate is added to the real count.
    Categories stay local estimates; only the total is anchored.
    """
    points = [
        dict(item) for item in (history or ())
        if isinstance(item, Mapping) and isinstance(item.get("estimated"), int)
    ][-HISTORY_LIMIT:]
    report = dict(breakdown)
    report["history"] = points
    used = report.get("used")
    anchor = next((item for item in reversed(points) if isinstance(item.get("actual"), int)), None)
    if anchor is None or not isinstance(used, int):
        return report
    report["calibrated_used"] = max(0, anchor["actual"] + used - anchor["estimated"])
    report["anchor"] = {"actual": anchor["actual"], "estimated": anchor["estimated"]}
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
        "turns": [],
        "note": "No model request has been assembled for this session yet.",
    }


def _category(name: str, group: str, tally: _Tally, total: int, detail: bool) -> dict[str, Any]:
    items = sorted(tally.items, key=lambda item: item["tokens"], reverse=True)
    category = {
        "id": name,
        "group": group,
        "tokens": tally.tokens,
        "share": (tally.tokens / total) if total else 0,
        "count": tally.count,
        "items": items if detail else items[:MAX_ITEMS],
    }
    if name in _BY_TOOL and tally.tools:
        category["by_tool"] = [
            {"label": tool, "count": calls, "tokens": tokens}
            for tool, (calls, tokens) in sorted(tally.tools.items(), key=lambda item: item[1][1], reverse=True)
        ][:MAX_ITEMS]
    return category


def _classify(message: dict[str, Any], source: str, walk: _Pass) -> None:
    role = message.get("role")
    content = _text(message)
    tokens = estimate_message_tokens(message)
    if role == "system":
        memory = _memory_block(content)
        memory_tokens = min(tokens, estimate_tokens(memory)) if memory else 0
        # Resident memory is appended to the system prompt, so it comes after it.
        _system_prompt(walk, content.replace(memory, "") if memory else content, tokens - memory_tokens)
        if memory_tokens:
            walk.base = "memory"
            walk.add("memory", memory_tokens, "Resident memory", ":resident", key="resident_memory", content=lambda: memory)
        return
    if role == "tool":
        _tool_result(message, tokens, walk)
        return
    if role == "assistant":
        _assistant(message, content, tokens, walk)
        return
    text = lambda: content  # noqa: E731
    if "wright-fact-recall" in content:
        walk.add("recalled_memory", tokens, "Recalled facts", key="recalled_facts", content=text)
    elif "wright-episode-recall" in content:
        walk.add("recalled_memory", tokens, "Recalled episodes", key="recalled_episodes", content=text)
    elif "<skill-catalog>" in content:
        walk.add("skills", tokens, "Skill catalog", key="skill_catalog", count=content.count("\n- "), content=text)
    elif "<plan-state>" in content:
        walk.add("plan", tokens, "Plan", key="plan", content=text)
    elif source == "transient":
        walk.add("reminders", tokens, _first_line(_strip_tags(content)) or "Reminder", prose=True, content=text)
    else:
        images = _image_parts(message)
        image_tokens = min(tokens, images * _IMAGE_TOKENS)
        label = _first_line(content)
        if image_tokens:
            walk.add("attachments", image_tokens, "Image", ":image", key="image", count=images, detail=label, content=text)
        if tokens - image_tokens > 0 or not image_tokens:
            walk.add("user", tokens - image_tokens, label or "Attachment", prose=bool(label), content=text)


def _system_prompt(walk: _Pass, content: str, tokens: int) -> None:
    """One item per section: role, tool guidance, general rules, and each headed block."""
    tally = walk.tallies["system_prompt"]
    sections = system_prompt_sections(content)
    walk.base = "system"
    for name, text in sections:
        key = f"system_{name}" if name in _SYSTEM_KEYS else _SYSTEM_HEADINGS.get(name, "system_section")
        walk.add(
            "system_prompt", estimate_tokens(text), _first_line(text) if name == "role" else name,
            f":{name}", key=key, prose=True, content=lambda text=text: text,
        )
    if not sections:
        walk.add("system_prompt", tokens, "System prompt", key="system_prompt")
    # The whole-message estimate is what the request budget uses.
    tally.tokens = tokens
    tally.count = 1


def _assistant(message: dict[str, Any], content: str, tokens: int, walk: _Pass) -> None:
    """Split a reply into its text, its reasoning, and the arguments of its tool calls."""
    reasoning = message.get("reasoning_content")
    reasoning_tokens = estimate_tokens(reasoning) if isinstance(reasoning, str) else 0
    parts: list[dict[str, Any]] = []
    call_tokens = 0
    for position, call in enumerate(message.get("tool_calls") or ()):
        name, summary = _call_summary(call)
        call_id = call.get("id") if isinstance(call, Mapping) else None
        call_id = call_id if isinstance(call_id, str) and call_id else ""
        if call_id:
            walk.calls[call_id] = (name, summary)
        size = estimate_tokens(json.dumps([call], ensure_ascii=False)) if isinstance(call, Mapping) else 0
        call_tokens += size
        parts.append(walk.add(
            "tool_calls", size, name, f":call:{call_id or position}", detail=summary, call_id=call_id,
            content=lambda call=call: _call_arguments(call),
        ))
        walk.tallies["tool_calls"].add_tool(name, size)
    if reasoning_tokens:
        parts.append(walk.add(
            "reasoning", reasoning_tokens, _first_line(reasoning) or "Reasoning", ":reasoning",
            prose=True, content=lambda: reasoning,
        ))
    # Whatever the parts do not account for is the reply text. Without text it
    # is only rounding, so it goes to the last part and every token is counted once.
    rest = max(0, tokens - reasoning_tokens - call_tokens)
    label = _first_line(content)
    if label or not parts:
        walk.add("assistant", rest, label or "Reply", ":text", prose=bool(label),
                 key="" if label else "empty_reply", content=lambda: content)
    else:
        parts[-1]["tokens"] += rest
        walk.tallies["reasoning" if reasoning_tokens else "tool_calls"].tokens += rest


def _tool_result(message: dict[str, Any], tokens: int, walk: _Pass) -> None:
    call_id = message.get("tool_call_id") if isinstance(message.get("tool_call_id"), str) else ""
    name, summary = walk.calls.get(call_id, (str(message.get("name") or "tool"), ""))
    payload = _payload(message)
    key = payload.get("retention_key") if isinstance(payload.get("retention_key"), str) else ""
    folded = bool(payload.get("folded"))
    content = lambda: _result_text(message, payload)  # noqa: E731
    if key.startswith("skill:"):
        # A loaded skill body stays in every later request as an instruction.
        walk.add("loaded_skills", tokens, key.removeprefix("skill:"), loaded=True, folded=folded,
                 call_id=call_id, content=content)
        return
    target = (
        "mcp_output" if mcp_server_of(name) is not None
        else "subagent_output" if name in _SUBAGENT_TOOLS
        else "tool_output"
    )
    walk.add(target, tokens, name, detail=summary, folded=folded, call_id=call_id, content=content)
    walk.tallies[target].add_tool(name, tokens)


def _tool_schemas(walk: _Pass, schemas: Any, total: int) -> None:
    tally = walk.tallies["tool_schemas"]
    mcp = walk.tallies["mcp_schemas"]
    walk.turn = None
    servers: dict[str, list[Mapping[str, Any]]] = {}
    for schema in schemas:
        if not isinstance(schema, Mapping):
            continue
        name = _schema_name(schema)
        server = mcp_server_of(name)
        if server is not None:
            servers.setdefault(server, []).append(schema)
            continue
        walk.base = f"schema:{name}"
        walk.add("tool_schemas", estimate_tools_tokens([dict(schema)]), name,
                 content=lambda schema=schema: _pretty(schema))
    for server, members in servers.items():
        walk.base = f"mcp:{server}"
        walk.add("mcp_schemas", estimate_tools_tokens([dict(item) for item in members]), server,
                 mcp=server, count=len(members), content=lambda members=members: _pretty(members))
    # The whole-list estimate is what the request budget uses; items are per
    # tool. MCP servers keep their own sums and built-in tools get the rest.
    mcp.tokens = min(total, mcp.tokens)
    tally.tokens = total - mcp.tokens


def _schema_name(schema: Mapping[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "")
    return str(schema.get("name") or "")


def _call_arguments(call: Any) -> str:
    if not isinstance(call, Mapping):
        return ""
    function = call.get("function") if isinstance(call.get("function"), Mapping) else call
    arguments = function.get("arguments", function.get("args"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return _pretty(arguments)


def _result_text(message: dict[str, Any], payload: dict[str, Any]) -> str:
    """The text the model sees, with a string result shown as written."""
    if isinstance(payload.get("data"), str) and "error" not in payload:
        return payload["data"]
    if payload:
        return _pretty(payload)
    return _text(message)


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


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


def _image_parts(message: dict[str, Any]) -> int:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return 0
    return sum(isinstance(part, dict) and part.get("type") == "image" for part in parts)


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


__all__ = [
    "HISTORY_LIMIT",
    "MAX_ITEMS",
    "MAX_TURNS",
    "calibrate",
    "classify_context",
    "context_detail",
    "empty_context",
    "history_entry",
]
