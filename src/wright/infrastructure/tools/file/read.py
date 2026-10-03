"""read_file tool implementation."""

from __future__ import annotations

import hashlib

from ....domain.model.tool import ToolResult
from ..base import Tool
from ..runtime import ToolRuntime
from .common import (
    FILE_UNCHANGED,
    MAX_READ_CHARS,
    _backend,
    _describe_file_read,
    _detect_encoding,
    _numbered_content,
    _remember_file_view,
    _remembered_file_view,
    _safe_path,
    _stamp_read_view,
    _unchanged_read_view,
)


def read_file(
    file: str,
    start_line: int = 1,
    start_column: int = 1,
    end_line: int | None = None,
    max_chars: int = 8000,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        if _backend(runtime).metadata(safe_path).kind != "file":
            return ToolResult.fail("Not a file", data={"content": ""})

        try:
            max_chars = int(max_chars)
        except (ValueError, TypeError):
            max_chars = 8000
        max_chars = max(0, min(max_chars, MAX_READ_CHARS))

        start_line = max(1, int(start_line))
        start_column = max(1, int(start_column))
        if end_line is not None:
            end_line = int(end_line)
            if end_line < start_line:
                return ToolResult.fail("end_line must be greater than or equal to start_line")

        viewed = _remembered_file_view(runtime, safe_path)
        stat = _backend(runtime).metadata(safe_path)
        encoding = _detect_encoding(safe_path, runtime)
        source_content = _backend(runtime).read_text(safe_path, encoding=encoding, errors="replace")
        digest = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
        if (viewed is not None and viewed.digest == digest) and _unchanged_read_view(
            viewed,
            mtime_ns=stat.modified_ns,
            size=stat.size,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            max_chars=max_chars,
        ):
            assert viewed is not None
            _remember_file_view(runtime, safe_path, viewed)
            return ToolResult.success({
                "content": FILE_UNCHANGED,
                "unchanged": True,
                "start_line": start_line,
                "start_column": start_column,
                "end_line": viewed.last_line,
                "next_start_line": viewed.next_start_line,
                "next_start_column": viewed.next_start_column,
                "truncated": viewed.truncated,
            })

        selected: list[str] = []
        total_chars = 0
        truncated = False
        last_line = start_line - 1
        next_start_line: int | None = None
        next_start_column: int | None = None
        source_lines = source_content.splitlines(keepends=True)
        for line_number, line in enumerate(source_lines, 1):
            runtime.raise_if_cancelled()
            if line_number < start_line:
                continue
            if end_line is not None and line_number > end_line:
                break
            visible_line = line[start_column - 1:] if line_number == start_line else line
            remaining = max_chars - total_chars
            if len(visible_line) > remaining:
                selected.append(visible_line[:remaining])
                total_chars = max_chars
                last_line = line_number
                truncated = True
                consumed_column = (
                    start_column + remaining
                    if line_number == start_line
                    else 1 + remaining
                )
                next_start_line = line_number
                next_start_column = consumed_column
                break
            selected.append(visible_line)
            total_chars += len(visible_line)
            last_line = line_number
        content = "".join(selected)
        _stamp_read_view(
            safe_path,
            runtime,
            content=content,
            source_content=source_content,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            max_chars=max_chars,
            truncated=truncated,
            last_line=last_line,
            next_start_line=next_start_line,
            next_start_column=next_start_column,
        )
        return ToolResult.success({
            "content": _numbered_content(content, start_line),
            "start_line": start_line,
            "start_column": start_column,
            "end_line": last_line,
            "next_start_line": next_start_line,
            "next_start_column": next_start_column,
            "truncated": truncated,
        })
    except Exception as e:
        return ToolResult.fail(str(e), data={"content": ""})


read_file_tool = Tool(
    name="read_file",
    description=(
        "Read a workspace file. Each returned line is prefixed with N| for reference; "
        "do not copy those prefixes into edit_file. An identical unchanged re-read "
        "returns a short stub."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Path relative to the session's current working directory",
            },
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "start_column": {"type": "integer", "minimum": 1, "default": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "max_chars": {"type": "integer", "minimum": 0, "maximum": MAX_READ_CHARS, "default": 8000},
        },
        "required": ["file"],
    },
    call=lambda args, runtime: read_file(**args, runtime=runtime),
    access_descriptor=_describe_file_read,
    is_concurrency_safe=lambda args: True,
    required_capabilities=frozenset({"execution"}),
)
