"""Directory listing, globbing, and content grep search tools."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ....domain.model.tool import ToolResult
from ..base import Tool
from ..runtime import ToolRuntime
from .common import (
    _backend,
    _describe_file_read,
    _safe_path,
)


def list_directory(
    directory: str = ".",
    include_hidden: bool = False,
    max_entries: int = 200,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_directory = _safe_path(directory, runtime)
        directory_meta = _backend(runtime).metadata(safe_directory)
        if directory_meta.kind != "directory":
            return ToolResult.fail("Not a directory", data={"entries": []})
        max_entries = max(1, min(int(max_entries), 2_000))
        entries = []
        for entry in sorted(_backend(runtime).iter_directory(safe_directory), key=lambda item: item.path.name.lower()):
            runtime.raise_if_cancelled()
            if not include_hidden and entry.path.name.startswith("."):
                continue
            kind = entry.metadata.kind
            entries.append({
                "name": entry.path.name,
                "path": _backend(runtime).display_path(entry.path),
                "type": kind,
                "size": entry.metadata.size if kind == "file" else None,
            })
            if len(entries) > max_entries:
                break
        truncated = len(entries) > max_entries
        entries = entries[:max_entries]
        return ToolResult.success({
            "directory": _backend(runtime).display_path(safe_directory),
            "entries": entries,
            "truncated": truncated,
        })
    except Exception as e:
        return ToolResult.fail(str(e), data={"entries": []})


def glob_files(
    pattern: str,
    directory: str = ".",
    include_hidden: bool = False,
    max_results: int = 200,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    """Find workspace paths by name/path pattern without invoking a shell."""
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            return ToolResult.fail("pattern must be a non-empty workspace-relative glob")
        root = _safe_path(directory, runtime)
        if _backend(runtime).metadata(root).kind != "directory":
            return ToolResult.fail("Not a directory", data={"matches": []})
        max_results = max(1, min(int(max_results), 2_000))
        matches: list[dict[str, Any]] = []
        backend = _backend(runtime)
        root_parts = tuple(part for part in root.value.rstrip("/").split("/") if part)
        for entry in backend.glob(root, pattern):
            runtime.raise_if_cancelled()
            displayed = backend.display_path(entry.path)
            hidden_parts = tuple(part for part in entry.path.value.rstrip("/").split("/") if part)[len(root_parts):]
            if not include_hidden and any(part.startswith(".") for part in hidden_parts):
                continue
            matches.append({
                "path": displayed,
                "type": entry.metadata.kind,
            })
        matches.sort(key=lambda item: item["path"])
        truncated = len(matches) > max_results
        return ToolResult.success({
            "matches": matches[:max_results],
            "truncated": truncated,
        })
    except Exception as e:
        return ToolResult.fail(str(e), data={"matches": []})


def grep_files(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    case_sensitive: bool = False,
    fixed_string: bool = False,
    max_results: int = 100,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    """Search file contents with ripgrep and return structured locations."""
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        if not pattern:
            return ToolResult.fail("pattern cannot be empty", data={"matches": []})
        target = _safe_path(path, runtime)
        if _backend(runtime).metadata(target).kind == "missing":
            return ToolResult.fail("Path does not exist", data={"matches": []})
        max_results = max(1, min(int(max_results), 2_000))
        matches: list[dict[str, Any]] = []
        truncated = False
        backend = _backend(runtime)
        deadline = time.monotonic() + 20
        candidates = backend.iter_search_candidates(
            target,
            glob=glob,
            deadline=deadline,
            cancellation_check=runtime.is_cancelled,
        )
        search = backend.search_files(
            candidates,
            pattern,
            case_sensitive=case_sensitive,
            fixed_string=fixed_string,
            deadline=deadline,
            cancellation_check=runtime.is_cancelled,
        )
        try:
            for match in search:
                runtime.raise_if_cancelled()
                matches.append({
                    "path": backend.display_path(match.path),
                    "line": match.line,
                    "column": match.column,
                    "text": match.text,
                })
                if len(matches) >= max_results:
                    truncated = True
                    break
        finally:
            close = getattr(search, "close", None)
            if callable(close):
                close()
            close = getattr(candidates, "close", None)
            if callable(close):
                close()
        return ToolResult.success({"matches": matches, "truncated": truncated})
    except Exception as e:
        return ToolResult.fail(str(e), data={"matches": []})


list_directory_tool = Tool(
    name="list_directory",
    description="List the immediate children of a known directory. Use glob to find paths recursively and grep to search file contents.",
    parameters={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Directory relative to the session's current working directory (default: .)",
            },
            "include_hidden": {"type": "boolean", "default": False},
            "max_entries": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
        },
    },
    call=lambda args, runtime: list_directory(**args, runtime=runtime),
    access_descriptor=_describe_file_read,
    is_concurrency_safe=lambda args: True,
    required_capabilities=frozenset({"execution"}),
)

glob_tool = Tool(
    name="glob",
    description="Find files or directories by a workspace-relative glob pattern. Use list_directory for one known directory and grep for content.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob such as **/*.py"},
            "directory": {"type": "string", "default": "."},
            "include_hidden": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
        },
        "required": ["pattern"],
    },
    call=lambda args, runtime: glob_files(**args, runtime=runtime),
    access_descriptor=_describe_file_read,
    is_concurrency_safe=lambda args: True,
    required_capabilities=frozenset({"execution"}),
)

grep_tool = Tool(
    name="grep",
    description="Search file contents with ripgrep and return path, line, column, and matching text.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "glob": {"type": "string", "description": "Optional file filter such as *.py"},
            "case_sensitive": {"type": "boolean", "default": False},
            "fixed_string": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 100},
        },
        "required": ["pattern"],
    },
    call=lambda args, runtime: grep_files(**args, runtime=runtime),
    access_descriptor=_describe_file_read,
    is_concurrency_safe=lambda args: True,
    required_capabilities=frozenset({"execution"}),
)
