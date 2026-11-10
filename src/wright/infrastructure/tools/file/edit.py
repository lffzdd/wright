"""edit tool implementation."""

from __future__ import annotations

from typing import Any

from ....domain.model.tool import ToolResult
from ....domain.model.tool_definition import Tool
from ....domain.model.tool_runtime import ToolRuntime
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
from .diff import edit_diff


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


MAX_EDITS = 50


def _normalized_edits(
    old_text: str | None, new_text: str | None, replace_all: bool, edits: list | None
) -> list[tuple[str, str, bool]] | str:
    """One shape for single and batched edits, or an error message."""
    if edits is not None:
        if old_text is not None or new_text is not None:
            return "pass either old_text/new_text or edits, not both"
        if not isinstance(edits, list) or not edits:
            return "edits must be a non-empty list"
        if len(edits) > MAX_EDITS:
            return f"at most {MAX_EDITS} edits per call"
        normalized = []
        for index, item in enumerate(edits):
            if not isinstance(item, dict) or not isinstance(item.get("old_text"), str) or not isinstance(item.get("new_text"), str):
                return f"edits[{index}] needs string old_text and new_text"
            normalized.append((item["old_text"], item["new_text"], bool(item.get("replace_all", False))))
        return normalized
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        return "old_text and new_text are required (or pass edits)"
    return [(old_text, new_text, bool(replace_all))]


def edit_file(
    file: str,
    old_text: str | None = None,
    new_text: str | None = None,
    replace_all: bool = False,
    edits: list | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)
        planned = _normalized_edits(old_text, new_text, replace_all, edits)
        if isinstance(planned, str):
            return ToolResult.fail(planned)
        batched = edits is not None

        with _path_lock(safe_path):
            runtime.raise_if_cancelled()
            if _backend(runtime).metadata(safe_path).kind != "file":
                return ToolResult.fail("Not a file")
            unread = _unread_or_stale(safe_path, runtime)
            if unread is not None:
                return unread

            content, encoding = _read_text(safe_path, runtime)
            updated = content
            replacements = 0
            # Apply in order to the in-memory text; any failure writes nothing.
            for index, (old, new, every) in enumerate(planned):
                prefix = f"edits[{index}]: " if batched else ""
                if not old:
                    return ToolResult.fail(f"{prefix}old_text must be non-empty")
                count = updated.count(old)
                if count == 0:
                    data = _not_found_data(updated, old)
                    if batched:
                        data["edit_index"] = index
                    return ToolResult.fail(f"{prefix}old_text not found", data=data)
                if count > 1 and not every:
                    data = _ambiguous_data(updated, old, count)
                    if batched:
                        data["edit_index"] = index
                    return ToolResult.fail(
                        f"{prefix}old_text found {count} times, replacement is ambiguous",
                        data=data,
                    )
                applied = count if every else 1
                updated = updated.replace(old, new, applied)
                replacements += applied
            _write_text(safe_path, updated, encoding, runtime)
            _stamp_write_view(safe_path, runtime, updated)

        return ToolResult.success({
            "message": "File updated",
            "file": _relative_file(safe_path, runtime),
            "replacements": replacements,
            **({"edits": len(planned)} if batched else {}),
            **edit_diff(content, updated, _relative_file(safe_path, runtime)),
        })
    except Exception as e:
        return ToolResult.fail(str(e))


def _approval_preview(args: dict) -> dict:
    edits = args.get("edits")
    if isinstance(edits, list):
        blocks = [
            f"replace:\n{item.get('old_text', '')}\nwith:\n{item.get('new_text', '')}"
            for item in edits if isinstance(item, dict)
        ]
        return {"preview": "\n\n".join(blocks)}
    return {"preview": "replace:\n" + str(args.get("old_text", "")) + "\nwith:\n" + str(args.get("new_text", ""))}


edit_file_tool = Tool(
    name="edit",
    approval_details=lambda args, cwd: _approval_preview(args),
    description=(
        "Replace exact text in an existing file. old_text must occur exactly once "
        "unless replace_all is true, and must not include the N| prefixes from "
        "read. Call read on this path first; if the file changed since "
        "that view, read it again. Failed matches return nearby file context in "
        "data.occurrences so you can widen old_text or re-read. For several changes "
        "to one file, pass edits (applied in order, all or nothing) instead of "
        "old_text/new_text. Prefer this for in-place edits. Use write to create a "
        "file or rewrite it whole."
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
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_EDITS,
                "description": "Several replacements in one file, applied in order; if any fails nothing is written",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["old_text", "new_text"],
                },
            },
        },
        "required": ["file"],
    },
    call=lambda args, runtime: edit_file(**args, runtime=runtime),
    access_descriptor=_describe_file_edit,
    required_capabilities=frozenset({"execution"}),
)
