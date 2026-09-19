# 文件操作工具链
import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..permission import PermissionCheckResult
from .base import Tool, ToolResult, ToolRuntime

MAX_READ_CHARS = 1_000_000  # 单次最多读 100 万字符,够用又不撑爆内存
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


def _workspace(runtime: ToolRuntime) -> Path:
    if runtime.capabilities is None or runtime.capabilities.execution is None:
        raise RuntimeError("file tool requires execution capability")
    return runtime.capabilities.execution.workspace_dir


def _backend(runtime: ToolRuntime):
    if runtime.capabilities is None or runtime.capabilities.execution is None:
        raise RuntimeError("file tool requires execution capability")
    return runtime.capabilities.execution


def _safe_path(path: str, runtime: ToolRuntime) -> Path:
    if runtime.capabilities is None or runtime.capabilities.execution is None:
        raise RuntimeError("file tool requires execution capability")
    return runtime.capabilities.execution.path(path)


def _path_lock(path: Path) -> threading.RLock:
    with _path_locks_guard:
        return _path_locks.setdefault(path, threading.RLock())


def _relative_file(path: Path, runtime: ToolRuntime) -> str:
    return str(path.relative_to(_workspace(runtime))) or "."


def _detect_encoding(path: Path, runtime: ToolRuntime) -> str:
    head = _backend(runtime).read_bytes(path, 4)
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


def _read_text(path: Path, runtime: ToolRuntime, *, replace: bool = False) -> tuple[str, str]:
    encoding = _detect_encoding(path, runtime)
    errors = "replace" if replace else "strict"
    return _backend(runtime).read_text(path, encoding=encoding, errors=errors), encoding


def _write_text(path: Path, content: str, encoding: str, runtime: ToolRuntime) -> None:
    _backend(runtime).write_text(path, content, encoding=encoding)


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


def _remember_file_view(runtime: ToolRuntime, path: Path, view: FileView) -> None:
    key = str(path.resolve())
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


def _remembered_file_view(runtime: ToolRuntime, path: Path) -> FileView | None:
    with runtime.scratch_lock:
        views = runtime.scratch.get(_FILE_VIEWS_KEY)
        if not views:
            return None
        return views.get(str(path.resolve()))


def _stamp_read_view(
    path: Path,
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
    stat = _backend(runtime).stat(path)
    _remember_file_view(
        runtime,
        path,
        FileView(
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
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


def _stamp_write_view(path: Path, runtime: ToolRuntime, content: str) -> None:
    stat = _backend(runtime).stat(path)
    _remember_file_view(
        runtime,
        path,
        FileView(
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            content=content,
            origin="write",
        ),
    )


def _unread_or_stale(
    path: Path,
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
    stat = _backend(runtime).stat(path)
    if stat.st_mtime_ns == viewed.mtime_ns and stat.st_size == viewed.size:
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


def list_directory(
    directory: str = ".",
    include_hidden: bool = False,
    max_entries: int = 200,
    runtime: ToolRuntime | None = None,
):
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_directory = _safe_path(directory, runtime)
        if not _backend(runtime).is_dir(safe_directory):
            return ToolResult.fail("Not a directory", data={"entries": []})
        max_entries = max(1, min(int(max_entries), 2_000))
        entries = []
        for entry in sorted(_backend(runtime).iter_directory(safe_directory), key=lambda item: item.name.lower()):
            runtime.raise_if_cancelled()
            if not include_hidden and entry.name.startswith("."):
                continue
            kind = "directory" if _backend(runtime).is_dir(entry) else "file" if _backend(runtime).is_file(entry) else "other"
            entries.append({
                "name": entry.name,
                "path": str(entry.relative_to(_workspace(runtime))),
                "type": kind,
                "size": _backend(runtime).stat(entry).st_size if kind == "file" else None,
            })
            if len(entries) > max_entries:
                break
        truncated = len(entries) > max_entries
        entries = entries[:max_entries]
        return ToolResult.success({
            "directory": str(safe_directory.relative_to(_workspace(runtime))) or ".",
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
):
    """Find workspace paths by name/path pattern without invoking a shell."""
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            return ToolResult.fail("pattern must be a non-empty workspace-relative glob")
        root = _safe_path(directory, runtime)
        if not _backend(runtime).is_dir(root):
            return ToolResult.fail("Not a directory", data={"matches": []})
        max_results = max(1, min(int(max_results), 2_000))
        matches: list[dict[str, Any]] = []
        for path in _backend(runtime).glob(root, pattern):
            runtime.raise_if_cancelled()
            if not path.resolve().is_relative_to(_workspace(runtime)):
                continue
            relative = path.relative_to(_workspace(runtime))
            if not include_hidden and any(part.startswith(".") for part in relative.parts):
                continue
            matches.append({
                "path": str(relative),
                "type": "directory" if _backend(runtime).is_dir(path) else "file" if _backend(runtime).is_file(path) else "other",
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
):
    """Search file contents with ripgrep and return structured locations."""
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        if not pattern:
            return ToolResult.fail("pattern cannot be empty", data={"matches": []})
        target = _safe_path(path, runtime)
        if not _backend(runtime).exists(target):
            return ToolResult.fail("Path does not exist", data={"matches": []})
        max_results = max(1, min(int(max_results), 2_000))
        command = ["rg", "--json", "--color", "never"]
        if not case_sensitive:
            command.append("--ignore-case")
        if fixed_string:
            command.append("--fixed-strings")
        if glob:
            command.extend(["--glob", glob])
        command.extend([pattern, str(target)])
        completed = _backend(runtime).run_process(
            command,
            cwd=_workspace(runtime),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            return ToolResult.fail(
                completed.stderr.strip() or f"ripgrep exited with {completed.returncode}",
                data={"matches": []},
            )
        matches: list[dict[str, Any]] = []
        truncated = False
        for raw_line in completed.stdout.splitlines():
            runtime.raise_if_cancelled()
            event = json.loads(raw_line)
            if event.get("type") != "match":
                continue
            data = event["data"]
            match_path = Path(data["path"]["text"])
            try:
                relative = match_path.resolve().relative_to(_workspace(runtime))
            except ValueError:
                continue
            line_number = int(data["line_number"])
            line_text = str(data["lines"]["text"]).rstrip("\r\n")
            submatches = data.get("submatches") or [{}]
            for submatch in submatches:
                matches.append({
                    "path": str(relative),
                    "line": line_number,
                    "column": int(submatch.get("start", 0)) + 1,
                    "text": line_text,
                })
                if len(matches) >= max_results:
                    truncated = True
                    break
            if truncated:
                break
        return ToolResult.success({"matches": matches, "truncated": truncated})
    except FileNotFoundError:
        return ToolResult.fail("ripgrep (rg) is not installed", data={"matches": []})
    except subprocess.TimeoutExpired:
        return ToolResult.fail("grep timed out after 20 seconds", data={"matches": []})
    except Exception as e:
        return ToolResult.fail(str(e), data={"matches": []})


def read_file(
    file: str,
    start_line: int = 1,
    start_column: int = 1,
    end_line: int | None = None,
    max_chars: int = 8000,
    runtime: ToolRuntime | None = None,
):
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        if not _backend(runtime).is_file(safe_path):
            return ToolResult.fail("Not a file", data={"content": ""})

        # max_chars 来自 LLM,可能是负数/字符串/小数/None。
        # clamp 策略:不让这个参数本身导致失败,统一夹回 [0, 上限]。
        try:
            max_chars = int(max_chars)  # str("8000")、float(8000.0) 都试着转
        except (ValueError, TypeError):
            max_chars = 8000  # 转不动(None、乱字符串)-> 回默认
        max_chars = max(0, min(max_chars, MAX_READ_CHARS))  # min 砍上限,max 托下限

        start_line = max(1, int(start_line))
        start_column = max(1, int(start_column))
        if end_line is not None:
            end_line = int(end_line)
            if end_line < start_line:
                return ToolResult.fail("end_line must be greater than or equal to start_line")

        viewed = _remembered_file_view(runtime, safe_path)
        stat = _backend(runtime).stat(safe_path)
        if _unchanged_read_view(
            viewed,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
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

        encoding = _detect_encoding(safe_path, runtime)
        selected: list[str] = []
        total_chars = 0
        truncated = False
        last_line = start_line - 1
        next_start_line: int | None = None
        next_start_column: int | None = None
        source_lines = _backend(runtime).read_text(
            safe_path, encoding=encoding, errors="replace"
        ).splitlines(keepends=True)
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


def write_file(
    file: str,
    content: str,
    overwrite: bool = True,
    runtime: ToolRuntime | None = None,
):
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        with _path_lock(safe_path):
            runtime.raise_if_cancelled()
            if _backend(runtime).exists(safe_path) and _backend(runtime).is_dir(safe_path):
                return ToolResult.fail("Path is a directory")

            if _backend(runtime).exists(safe_path) and not overwrite:
                return ToolResult.fail("File already exists")
            if _backend(runtime).is_file(safe_path):
                blocked = _unread_or_stale(
                    safe_path, runtime, require_complete=True
                )
                if blocked is not None:
                    return blocked

            encoding = (
                _detect_encoding(safe_path, runtime) if _backend(runtime).is_file(safe_path) else "utf-8"
            )
            _backend(runtime).ensure_directory(safe_path.parent)
            _write_text(safe_path, content, encoding, runtime)
            _stamp_write_view(safe_path, runtime, content)

        return ToolResult.success(
            {
                "message": "File written",
                "file": str(safe_path.relative_to(_workspace(runtime))),
                "chars": len(content),
            }
        )
    except Exception as e:
        return ToolResult.fail(str(e))


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
):
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        with _path_lock(safe_path):
            runtime.raise_if_cancelled()
            if not _backend(runtime).is_file(safe_path):
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
            "file": str(safe_path.relative_to(_workspace(runtime))),
            "replacements": replacements,
        })
    except Exception as e:
        return ToolResult.fail(str(e))


def _ask_file_write(args: dict, runtime) -> PermissionCheckResult:
    flags = ("writes_files",)
    return PermissionCheckResult(
        "ask",
        f"{runtime.tool_name}: requires user approval by file tool policy; risks={', '.join(flags)}",
        flags,
        source="tool",
    )


def _ask_file_edit(args: dict, runtime) -> PermissionCheckResult:
    flags = ("reads_files", "writes_files")
    return PermissionCheckResult(
        "ask",
        f"{runtime.tool_name}: requires user approval by file tool policy; risks={', '.join(flags)}",
        flags,
        source="tool",
    )


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
    is_concurrency_safe=lambda args: True,
    required_capabilities=frozenset({"execution"}),
)

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
    is_concurrency_safe=lambda args: True,
    required_capabilities=frozenset({"execution"}),
)

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
    check_permission=_ask_file_write,
    required_capabilities=frozenset({"execution"}),
)


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
    check_permission=_ask_file_edit,
    required_capabilities=frozenset({"execution"}),
)
