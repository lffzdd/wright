"""edit_file tool implementation."""

from __future__ import annotations

from typing import Any

from ....domain.model.tool import ToolResult
from ..base import Tool
from ..runtime import ToolRuntime
from .common import (
    _EDIT_OCCURRENCE_LIMIT,
    _EDIT_PREVIEW_LINE_CHARS,
    _EDIT_PREVIEW_RADIUS,
    _backend,
    _describe_file_edit,
    _path_lock,
    _read_text,
    _relative_file,
    _safe_path,
    _stamp_write_view,
    _unread_or_stale,
    _write_text,
)


def _line_count(content: str) -> int:
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def _preview_lines(lines: list[str], line_no: int) -> str:
    start = max(0, line_no - 1 - _EDIT_PREVIEW_RADIUS)
    end = min(len(lines), line_no + _EDIT_PREVIEW_RADIUS)
    rows = []
    for index in range(start, end):
        text = lines[index]
        if len(text) > _EDIT_PREVIEW_LINE_CHARS:
            text = text[:_EDIT_PREVIEW_LINE_CHARS] + "…"
        rows.append(f"{index + 1}|{text}")
    return "\n".join(rows)


def _occurrences(
    content: str, needle: str, *, limit: int = _EDIT_OCCURRENCE_LIMIT
) -> tuple[list[dict[str, Any]], bool]:
    if not needle:
        return [], False
    lines = content.splitlines()
    found: list[dict[str, Any]] = []
    start = 0
    step = max(len(needle), 1)
    truncated = False
    while True:
        index = content.find(needle, start)
        if index < 0:
            break
        if len(found) >= limit:
            truncated = True
            break
        line_no = content.count("\n", 0, index) + 1
        found.append({"line": line_no, "preview": _preview_lines(lines, line_no)})
        start = index + step
    return found, truncated


def _not_found_data(content: str, old_text: str) -> dict[str, Any]:
    stripped = old_text.strip()
    whitespace_differs = bool(
        stripped and stripped != old_text and content.count(stripped)
    )
    needle = stripped if whitespace_differs else ""
    occurrences, truncated = _occurrences(content, needle) if needle else ([], False)
    return {
        "reason": "not_found",
        "line_count": _line_count(content),
        "whitespace_differs": whitespace_differs,
        "occurrences": occurrences,
        "truncated": truncated,
    }


def _ambiguous_data(content: str, old_text: str, count: int) -> dict[str, Any]:
    occurrences, truncated = _occurrences(content, old_text)
    return {
        "reason": "ambiguous",
        "count": count,
        "occurrences": occurrences,
        "truncated": truncated,
    }


def edit_file(
    file: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        with _path_lock(safe_path):
            runtime.raise_if_cancelled()
            if _backend(runtime).metadata(safe_path).kind != "file":
                return ToolResult.fail("Not a file")
            if not old_text:
                return ToolResult.fail("old_text must be non-empty")
            unread = _unread_or_stale(safe_path, runtime)
            if unread is not None:
                return unread

            content, encoding = _read_text(safe_path, runtime)
            count = content.count(old_text)
            if count == 0:
                return ToolResult.fail(
                    "old_text not found", data=_not_found_data(content, old_text)
                )
            if count > 1 and not replace_all:
                return ToolResult.fail(
                    f"old_text found {count} times, replacement is ambiguous",
                    data=_ambiguous_data(content, old_text, count),
                )

            replacements = count if replace_all else 1
            updated = content.replace(old_text, new_text, replacements)
            _write_text(safe_path, updated, encoding, runtime)
            _stamp_write_view(safe_path, runtime, updated)

        return ToolResult.success({
            "message": "File updated",
            "file": _relative_file(safe_path, runtime),
            "replacements": replacements,
        })
    except Exception as e:
        return ToolResult.fail(str(e))


edit_file_tool = Tool(
    name="edit_file",
    description=(
        "Replace exact text in an existing file. old_text must occur exactly once "
        "unless replace_all is true, and must not include the N| prefixes from "
        "read_file. Call read_file on this path first; if the file changed since "
        "that view, read it again. Failed matches return nearby file context in "
        "data.occurrences so you can widen old_text or re-read. Prefer this for "
        "in-place edits. Use write_file to create a file or rewrite it whole."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Path relative to the session's current working directory",
            },
            "old_text": {
                "type": "string",
                "description": "The exact text to replace; must match once unless replace_all is true",
            },
            "new_text": {
                "type": "string",
                "description": "The replacement text",
            },
            "replace_all": {
                "type": "boolean",
                "default": False,
                "description": "Replace every occurrence instead of requiring a unique match",
            },
        },
        "required": ["file", "old_text", "new_text"],
    },
    call=lambda args, runtime: edit_file(**args, runtime=runtime),
    access_descriptor=_describe_file_edit,
    required_capabilities=frozenset({"execution"}),
)
