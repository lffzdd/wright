"""Ordered execution timeline projected from session records.

Item identity comes from message ids and tool call ids. Text in a transcript
does not by itself mark a sub-agent or a tool as finished.
"""

from __future__ import annotations

from typing import Any

_SHELL = frozenset({"execute_command"})
_EDIT = frozenset({"edit_file", "write_file"})


def project_timeline(session: Any, pending: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
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
            text = _text(message)
            if text:
                items.append({
                    "id": getattr(record, "id", f"assistant:{index}"),
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
    for offset, interaction in enumerate(pending or []):
        items.append({
            "id": str(interaction.get("request_id") or f"approval:{offset}"),
            "kind": "approval",
            "phase": "pending",
            "interaction": interaction,
            "order": len(records) + len(ordered) + offset,
        })
    return items


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
        "phase": str(getattr(execution, "status", "")),
        "run_id": getattr(execution, "run_id", ""),
        "duration_ms": duration_ms,
        "result": _public_result(result),
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
        access = "write" if name in _EDIT else "read" if name in {"read_file", "grep", "glob"} else "touch"
        for path in paths:
            current = found.get(path)
            if current is None or (current["access"] != "write" and access == "write"):
                found[path] = {"path": path, "access": access, "tool": name, "call_id": call.id}
    return list(found.values())


def _paths(arguments: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("file", "path", "target"):
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


def _public_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    cleaned = {key: value for key, value in result.items() if key != "storage_path"}
    return cleaned


__all__ = ["project_accessed_files", "project_subagents", "project_timeline"]
