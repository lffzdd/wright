"""write_file tool implementation."""

from __future__ import annotations

from ....domain.model.tool import ToolResult
from ..base import Tool
from ..runtime import ToolRuntime
from .common import (
    _backend,
    _describe_file_write,
    _detect_encoding,
    _path_lock,
    _relative_file,
    _safe_path,
    _stamp_write_view,
    _unread_or_stale,
    _write_text,
)


def write_file(
    file: str,
    content: str,
    overwrite: bool = True,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        with _path_lock(safe_path):
            runtime.raise_if_cancelled()
            existing = _backend(runtime).metadata(safe_path)
            if existing.kind == "directory":
                return ToolResult.fail("Path is a directory")

            if existing.kind != "missing" and not overwrite:
                return ToolResult.fail("File already exists")
            if existing.kind == "file":
                blocked = _unread_or_stale(
                    safe_path, runtime, require_complete=True
                )
                if blocked is not None:
                    return blocked

            encoding = (
                _detect_encoding(safe_path, runtime) if existing.kind == "file" else "utf-8"
            )
            _backend(runtime).ensure_directory(safe_path.parent)
            _write_text(safe_path, content, encoding, runtime)
            _stamp_write_view(safe_path, runtime, content)

        return ToolResult.success(
            {
                "message": "File written",
                "file": _relative_file(safe_path, runtime),
                "chars": len(content),
            }
        )
    except Exception as e:
        return ToolResult.fail(str(e))


write_file_tool = Tool(
    name="write_file",
    description=(
        "Create or overwrite a workspace file. Creates parent directories. "
        "Overwriting an existing file requires a complete read_file first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Path relative to the session's current working directory",
            },
            "content": {
                "type": "string",
                "description": "The full content to write to the file",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Whether to overwrite the file if it already exists",
                "default": True,
            },
        },
        "required": ["file", "content"],
    },
    call=lambda args, runtime: write_file(**args, runtime=runtime),
    access_descriptor=_describe_file_write,
    required_capabilities=frozenset({"execution"}),
)
