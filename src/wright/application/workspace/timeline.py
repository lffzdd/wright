"""Ordered execution timeline projected from session records.

Item identity comes from message ids and tool call ids. Text in a transcript
does not by itself mark a sub-agent or a tool as finished.
"""

from __future__ import annotations

from typing import Any

from ..session_runtime.tool_projection import public_tool_result, tool_phase

_SHELL = frozenset({"execute_command"})
_EDIT = frozenset({"edit_file", "write_file"})
# Directory-level tools: their target is a folder, not a file to preview.
_LIST = frozenset({"list_directory", "glob"})


def project_timeline(
    session: Any, pending: list[dict[str, Any]] | None = None,
    *, live_turn: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    records = getattr(session, "message_records", None)
    if not isinstance(records, list):
        return []
    executions = getattr(session, "tool_executions", {}) or {}
    items: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    for index, record in enumerate(records):
        message = getattr(record, "message", {}) or {}
        role = message.get("role")
        source = getattr(record, "source", "")
        if role == "user" and source == "user_input":
            items.append({
                "id": getattr(record, "id", f"user:{index}"),
                "kind": "text",
                "role": "user",
                "text": _text(message),
                "order": index,
            })
        elif role == "assistant":
            record_id = getattr(record, "id", f"assistant:{index}")
            reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "").strip()
            if reasoning:
                items.append({
                    "id": f"{record_id}:reason",
                    "kind": "reasoning",
                    "role": "assistant",
                    "text": reasoning,
                    "order": index,
                })
            text = _text(message)
            if text:
                items.append({
                    "id": record_id,
                    "kind": "text",
                    "role": "assistant",
                    "text": text,
                    "order": index,
                })
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                execution = executions.get(call_id)
                if call_id and execution is not None and call_id not in seen_calls:
                    items.append(_tool_item(execution, index))
                    seen_calls.add(call_id)
    ordered = sorted(
        executions.values(),
        key=lambda item: (getattr(item, "step", 0), str(getattr(getattr(item, "call", None), "id", ""))),
    )
    for offset, execution in enumerate(ordered):
        call_id = str(getattr(getattr(execution, "call", None), "id", ""))
        if call_id in seen_calls:
            continue
        items.append(_tool_item(execution, len(records) + offset))
    items = _with_run_outcomes(session, items)
    # The live fold owns this turn until its terminal event. Records may already
    # contain a completed model step, so replace the suffix rather than append
    # another copy of its reasoning and tools. The boundary is captured before
    # the user command starts, not inferred from possibly repeated prompt text.
    if live_turn is not None:
        start = live_turn.get("message_index")
        timeline = live_turn.get("timeline")
        # Only a real index is a split point. bool is an int subclass, and a
        # fold without this boundary must not drop earlier history.
        if type(start) is int and isinstance(timeline, list):
            session_start = getattr(session, "active_turn_start_message_index", None)
            if type(session_start) is int and session_start > start:
                start = session_start
            items = [item for item in items if item.get("order", 0) < start]
            items.extend(timeline)
    for offset, interaction in enumerate(pending or []):
        items.append({
            "id": str(interaction.get("request_id") or f"approval:{offset}"),
            "kind": "approval",
            "phase": "pending",
            "interaction": interaction,
            "order": len(records) + len(ordered) + offset,
        })
    return items


_UNSUCCESSFUL = frozenset({"failed", "cancelled", "interrupted"})


def _with_run_outcomes(session: Any, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert one status item where each failed or cancelled user turn ended.

    Runs are created in the order people sent messages, so the n-th user Run
    belongs to the n-th user message. Alignment starts from the newest turn,
    which keeps recent history right even if an old Run never recorded input.
    """
    runs = [
        run for run in (getattr(session, "runs", {}) or {}).values()
        if getattr(run, "source", "") == "user_input"
    ]
    user_positions = [
        index for index, item in enumerate(items)
        if item.get("kind") == "text" and item.get("role") == "user"
    ]
    if not runs or not user_positions:
        return items
    pairs = list(zip(reversed(user_positions), reversed(runs), strict=False))
    inserts: dict[int, dict[str, Any]] = {}
    boundaries = [*user_positions[1:], len(items)]
    end_of = dict(zip(user_positions, boundaries, strict=True))
    for position, run in pairs:
        status = str(getattr(run, "status", ""))
        if status not in _UNSUCCESSFUL:
            continue
        inserts[end_of[position]] = {
            "id": f"{run.run_id}:status",
            "kind": "status",
            "status": status,
            "run_id": run.run_id,
            "text": str(getattr(run, "error", "") or "") if status != "cancelled" else "",
            "order": items[end_of[position] - 1].get("order", 0) if end_of[position] else 0,
        }
    if not inserts:
        return items
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if index in inserts:
            result.append(inserts[index])
        result.append(item)
    if len(items) in inserts:
        result.append(inserts[len(items)])
    return result


def _tool_item(execution: Any, order: int) -> dict[str, Any]:
    call = execution.call
    name = str(call.name)
    if name in _SHELL:
        kind = "shell"
    elif name in _EDIT:
        kind = "edit"
    else:
        kind = "tool"
    started = getattr(execution, "started_at", None)
    ended = getattr(execution, "ended_at", None)
    duration_ms = None
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)) and ended >= started:
        duration_ms = int((ended - started) * 1000)
    result = execution.result.to_dict() if getattr(execution, "result", None) is not None else None
    return {
        "id": call.id,
        "kind": kind,
        "name": name,
        "call_id": call.id,
        "arguments": dict(call.arguments),
        "phase": tool_phase(getattr(execution, "status", "")),
        "run_id": getattr(execution, "run_id", ""),
        "duration_ms": duration_ms,
        "result": public_tool_result(result),
        "order": order,
    }


def project_subagents(session: Any) -> list[dict[str, Any]]:
    plane = getattr(session, "control_plane", None)
    tree = getattr(plane, "tree", None)
    if not callable(tree):
        return []
    records = tree(None)
    if not isinstance(records, list):
        return []
    projected = []
    for item in _flatten(records):
        if item.get("parent_id") in (None, ""):
            continue
        projected.append({
            "task_id": item.get("id") or item.get("task_id"),
            "parent_id": item.get("parent_id"),
            "status": item.get("status"),
            "task": item.get("task"),
            "ended_at": item.get("ended_at"),
        })
    return projected


def _flatten(nodes: list[Any]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for item in nodes:
        if not isinstance(item, dict):
            continue
        flat.append(item)
        children = item.get("children")
        if isinstance(children, list) and children and isinstance(children[0], dict):
            flat.extend(_flatten(children))
    return flat


def project_accessed_files(session: Any) -> list[dict[str, Any]]:
    executions = getattr(session, "tool_executions", {}) or {}
    found: dict[str, dict[str, Any]] = {}
    for execution in executions.values():
        call = getattr(execution, "call", None)
        if call is None:
            continue
        paths = _paths(dict(getattr(call, "arguments", {}) or {}))
        name = str(call.name)
        # Incomplete or failed tools have not successfully touched workspace files.
        if tool_phase(getattr(execution, "status", "")) != "succeeded":
            continue
        access = (
            "write" if name in _EDIT
            else "read" if name in {"read_file", "grep"}
            else "list" if name in _LIST
            else "touch"
        )
        for path in paths:
            # Re-accessing a path moves it to the end, so the list runs oldest to newest by last access.
            previous = found.pop(path, None)
            wrote = access == "write" or (previous is not None and previous["access"] == "write")
            # Per-tool counts, in first-use order, so clients can show every kind of access.
            tools = dict(previous["tools"]) if previous else {}
            tools[name] = tools.get(name, 0) + 1
            # The latest call per tool, most recent last, so a client can jump to the edit rather than a later read.
            last_calls = dict(previous["last_calls"]) if previous else {}
            last_calls.pop(name, None)
            last_calls[name] = call.id
            found[path] = {
                "path": path,
                "access": "write" if wrote else access,
                "tool": name,
                "call_id": call.id,
                "count": (previous["count"] if previous else 0) + 1,
                "tools": tools,
                "last_calls": last_calls,
            }
    return list(found.values())


def _paths(arguments: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("file", "path", "target", "directory"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    many = arguments.get("paths")
    if isinstance(many, list):
        values.extend(str(item) for item in many if isinstance(item, str) and item)
    return values


def _text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    return content.strip() if isinstance(content, str) else ""


__all__ = ["project_accessed_files", "project_subagents", "project_timeline"]
