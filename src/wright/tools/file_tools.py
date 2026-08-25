# 文件操作工具链
from pathlib import Path
import threading
from ..permission import PermissionCheckResult
from .base import Tool, ToolResult, ToolRuntime

MAX_READ_CHARS = 1_000_000  # 单次最多读 100 万字符,够用又不撑爆内存

_path_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.RLock] = {}


def _workspace(runtime: ToolRuntime) -> Path:
    if runtime.workspace_dir is None:
        raise RuntimeError("file tool requires runtime.workspace_dir")
    return runtime.workspace_dir.resolve()


def _safe_path(path: str, runtime: ToolRuntime) -> Path:
    workspace = _workspace(runtime)
    safe_path = (workspace / path).resolve()
    if not safe_path.is_relative_to(workspace):
        raise ValueError("Unsafe path")
    return safe_path


def _path_lock(path: Path) -> threading.RLock:
    with _path_locks_guard:
        return _path_locks.setdefault(path, threading.RLock())


def list_files(directory: str = ".", runtime: ToolRuntime | None = None):
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_directory = _safe_path(directory, runtime)
        files = [entry.name for entry in safe_directory.iterdir() if entry.is_file()]
        dirs = [entry.name for entry in safe_directory.iterdir() if entry.is_dir()]
        return ToolResult.success({"files": files, "dirs": dirs})
    except Exception as e:
        return ToolResult.fail(str(e), data={"files": [], "dirs": []})


def read_file(
    file: str, max_chars: int = 8000, runtime: ToolRuntime | None = None
):
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        if not safe_path.is_file():
            return ToolResult.fail("Not a file", data={"content": ""})

        # max_chars 来自 LLM,可能是负数/字符串/小数/None。
        # clamp 策略:不让这个参数本身导致失败,统一夹回 [0, 上限]。
        try:
            max_chars = int(max_chars)  # str("8000")、float(8000.0) 都试着转
        except (ValueError, TypeError):
            max_chars = 8000  # 转不动(None、乱字符串)-> 回默认
        max_chars = max(0, min(max_chars, MAX_READ_CHARS))  # min 砍上限,max 托下限

        with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 1)

        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]

        return ToolResult.success({"content": content, "truncated": truncated})
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
            if safe_path.exists() and safe_path.is_dir():
                return ToolResult.fail("Path is a directory")

            if safe_path.exists() and not overwrite:
                return ToolResult.fail("File already exists")

            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.write_text(content, encoding="utf-8")

        return ToolResult.success(
            {
                "message": "File written",
                "file": str(safe_path.relative_to(_workspace(runtime))),
                "chars": len(content),
            }
        )
    except Exception as e:
        return ToolResult.fail(str(e))


def edit_file(
    file: str,
    old_text: str,
    new_text: str,
    runtime: ToolRuntime | None = None,
):
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        with _path_lock(safe_path):
            runtime.raise_if_cancelled()
            if not safe_path.is_file():
                return ToolResult.fail("Not a file")

            # 必须在锁内重读，避免等待锁期间文件内容已被其他 Agent 改变。
            content = safe_path.read_text(encoding="utf-8")

            count = content.count(old_text)
            if count == 0:
                return ToolResult.fail("old_text not found")
            if count > 1:
                return ToolResult.fail(
                    f"old_text found {count} times, replacement is ambiguous"
                )

            updated = content.replace(old_text, new_text, 1)
            safe_path.write_text(updated, encoding="utf-8")

        return ToolResult.success({"message": "File updated"})
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


list_files_tool = Tool(
    name="list_files",
    description="List all files in a specified directory.",
    parameters={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "The directory to list files in",
            },
        },
        "required": ["directory"],
    },
    call=lambda args, runtime: list_files(**args, runtime=runtime),
    is_concurrency_safe=lambda args: True,
)

read_file_tool = Tool(
    name="read_file",
    description="Read the content of a file in the workspace. Returns the text content of the file.",
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "The relative path of the file to read",
            },
        },
        "required": ["file"],
    },
    call=lambda args, runtime: read_file(**args, runtime=runtime),
    is_concurrency_safe=lambda args: True,
)

write_file_tool = Tool(
    name="write_file",
    description="Write content to a file inside the workspace. Creates parent directories if needed.",
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "The relative path of the file to write",
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
)


edit_file_tool = Tool(
    name="edit_file",
    description="Replace exact text in a file inside the workspace.",
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "The file path inside the workspace",
            },
            "old_text": {
                "type": "string",
                "description": "The exact text to replace",
            },
            "new_text": {
                "type": "string",
                "description": "The replacement text",
            },
        },
        "required": ["file", "old_text", "new_text"],
    },
    call=lambda args, runtime: edit_file(**args, runtime=runtime),
    check_permission=_ask_file_edit,
)
