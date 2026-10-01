"""Shared infrastructure utilities, caching, and access descriptors for file tools."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ....domain.model.tool import AccessTarget, ToolAccess, ToolResult
from ...runtime import AuthorizedExecution, ExecutionPath
from ..runtime import ToolRuntime

MAX_READ_CHARS = 1_000_000
FILE_UNCHANGED = "File unchanged since last read."
_EDIT_OCCURRENCE_LIMIT = 5
_EDIT_PREVIEW_RADIUS = 2
_EDIT_PREVIEW_LINE_CHARS = 200
_FILE_VIEWS_KEY = "file_views"
_MAX_FILE_VIEWS = 100
_MAX_FILE_VIEW_CHARS = 8_000_000
FileViewOrigin = Literal["read", "write"]

_path_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.RLock] = {}


def _backend(runtime: ToolRuntime) -> AuthorizedExecution:
    if runtime.execution is None:
        raise RuntimeError("file tool requires an invocation authorization")
    return runtime.execution


def _safe_path(path: str, runtime: ToolRuntime) -> ExecutionPath:
    return _backend(runtime).resolve_path(path)


def _path_lock(path: ExecutionPath) -> threading.RLock:
    with _path_locks_guard:
        return _path_locks.setdefault(Path(path.value), threading.RLock())


def _relative_file(path: ExecutionPath, runtime: ToolRuntime) -> str:
    return _backend(runtime).display_path(path)


def _detect_encoding(path: ExecutionPath, runtime: ToolRuntime) -> str:
    head = _backend(runtime).read_bytes(path, 4)
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


def _read_text(path: ExecutionPath, runtime: ToolRuntime, *, replace: bool = False) -> tuple[str, str]:
    encoding = _detect_encoding(path, runtime)
    errors = "replace" if replace else "strict"
    return _backend(runtime).read_text(path, encoding=encoding, errors=errors), encoding


def _write_text(path: ExecutionPath, content: str, encoding: str, runtime: ToolRuntime) -> None:
    journal = getattr(runtime, "file_journal", None)
    before = None
    existed = False
    absolute = Path(path.value)
    if journal is not None and absolute.is_file():
        existed = True
        try:
            before = _backend(runtime).read_text(path, encoding=encoding)
        except (OSError, UnicodeError):
            before = None
    _backend(runtime).write_text(path, content, encoding=encoding)
    if journal is None:
        return
    try:
        journal.note_text(
            absolute,
            before=before,
            after=content,
            existed=existed,
            call_id=getattr(runtime, "tool_call_id", ""),
            tool_name=getattr(runtime, "tool_name", ""),
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("change journal missed a file write")


@dataclass(frozen=True)
class FileView:
    """Process-local snapshot of a file the model has already seen or written."""

    mtime_ns: int
    size: int
    content: str
    origin: FileViewOrigin
    start_line: int | None = None
    start_column: int | None = None
    end_line: int | None = None
    max_chars: int | None = None
    truncated: bool = False
    last_line: int | None = None
    next_start_line: int | None = None
    next_start_column: int | None = None

    @property
    def is_complete(self) -> bool:
        if self.origin == "write":
            return True
        return (
            self.start_line == 1
            and (self.start_column or 1) == 1
            and self.end_line is None
            and not self.truncated
        )


def _file_views(runtime: ToolRuntime) -> dict[str, FileView]:
    views = runtime.scratch.get(_FILE_VIEWS_KEY)
    if views is None:
        views = {}
        runtime.scratch[_FILE_VIEWS_KEY] = views
    return views


def _remember_file_view(runtime: ToolRuntime, path: ExecutionPath, view: FileView) -> None:
    key = path.value
    with runtime.scratch_lock:
        views = _file_views(runtime)
        views.pop(key, None)
        views[key] = view
        total = sum(len(item.content) for item in views.values())
        while len(views) > _MAX_FILE_VIEWS or (
            total > _MAX_FILE_VIEW_CHARS and len(views) > 1
        ):
            oldest_key, evicted = next(iter(views.items()))
            if oldest_key == key:
                break
            del views[oldest_key]
            total -= len(evicted.content)


def _remembered_file_view(runtime: ToolRuntime, path: ExecutionPath) -> FileView | None:
    with runtime.scratch_lock:
        views = runtime.scratch.get(_FILE_VIEWS_KEY)
        if not views:
            return None
        return views.get(path.value)


def _stamp_read_view(
    path: ExecutionPath,
    runtime: ToolRuntime,
    *,
    content: str,
    start_line: int,
    start_column: int,
    end_line: int | None,
    max_chars: int,
    truncated: bool,
    last_line: int,
    next_start_line: int | None,
    next_start_column: int | None,
) -> None:
    stat = _backend(runtime).metadata(path)
    _remember_file_view(
        runtime,
        path,
        FileView(
            mtime_ns=stat.modified_ns,
            size=stat.size,
            content=content,
            origin="read",
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            max_chars=max_chars,
            truncated=truncated,
            last_line=last_line,
            next_start_line=next_start_line,
            next_start_column=next_start_column,
        ),
    )


def _stamp_write_view(path: ExecutionPath, runtime: ToolRuntime, content: str) -> None:
    stat = _backend(runtime).metadata(path)
    _remember_file_view(
        runtime,
        path,
        FileView(
            mtime_ns=stat.modified_ns,
            size=stat.size,
            content=content,
            origin="write",
        ),
    )


def _unread_or_stale(
    path: ExecutionPath,
    runtime: ToolRuntime,
    *,
    require_complete: bool = False,
) -> ToolResult | None:
    relative = _relative_file(path, runtime)
    viewed = _remembered_file_view(runtime, path)
    if viewed is None:
        return ToolResult.fail(
            "read_file this path first",
            data={"reason": "not_read", "file": relative},
        )
    if require_complete and not viewed.is_complete:
        return ToolResult.fail(
            "read the whole file before overwriting it",
            data={"reason": "incomplete", "file": relative},
        )
    stat = _backend(runtime).metadata(path)
    if stat.modified_ns == viewed.mtime_ns and stat.size == viewed.size:
        return None
    if viewed.is_complete and _read_text(path, runtime, replace=True)[0] == viewed.content:
        return None
    return ToolResult.fail(
        "file changed since last read_file; read it again",
        data={"reason": "stale", "file": relative},
    )


def _numbered_content(content: str, start_line: int) -> str:
    if not content:
        return ""
    parts: list[str] = []
    line_no = start_line
    start = 0
    while start < len(content):
        newline = content.find("\n", start)
        if newline < 0:
            parts.append(f"{line_no}|{content[start:]}")
            break
        parts.append(f"{line_no}|{content[start:newline]}\n")
        line_no += 1
        start = newline + 1
    return "".join(parts)


def _unchanged_read_view(
    viewed: FileView | None,
    *,
    mtime_ns: int,
    size: int,
    start_line: int,
    start_column: int,
    end_line: int | None,
    max_chars: int,
) -> bool:
    return (
        viewed is not None
        and viewed.origin == "read"
        and viewed.mtime_ns == mtime_ns
        and viewed.size == size
        and viewed.start_line == start_line
        and (viewed.start_column or 1) == start_column
        and viewed.end_line == end_line
        and viewed.max_chars == max_chars
    )


def _path_argument(args: dict) -> str:
    for key in ("file", "directory", "path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return "."


def _file_access(
    args: dict,
    *,
    operations: frozenset,
    recursive: bool = False,
    reason: str,
) -> ToolAccess:
    key = "directory" if "directory" in args else "path" if "path" in args else "file"
    value = _path_argument(args)
    targets = tuple(
        AccessTarget(key, value, operation, recursive, "directory" if recursive else "file")
        for operation in operations
    )
    flags = tuple(
        flag
        for flag, operation in (("reads_files", "file_read"), ("writes_files", "file_write"))
        if operation in operations
    )
    return ToolAccess(operations, targets, subject=value, risk_flags=flags, reason=reason)


def _describe_file_read(args: dict) -> ToolAccess:
    recursive = "file" not in args
    return _file_access(
        args,
        operations=frozenset({"file_read"}),
        recursive=recursive,
        reason="read files or directory entries",
    )


def _describe_file_write(args: dict) -> ToolAccess:
    return _file_access(
        args,
        operations=frozenset({"file_read", "file_write"}),
        reason="read and write the requested file",
    )


def _describe_file_edit(args: dict) -> ToolAccess:
    return _describe_file_write(args)
