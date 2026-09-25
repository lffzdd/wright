import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..runtime import ExecutionPath, ProcessHandle
from ...core.logger import get_logger
from ...core.processes import ProcessResources
from ...domain.model.tool import ToolResult
from .base import Tool
from .command_permissions import (
    describe_execute_command_access,
    is_execute_command_concurrency_safe,
)
from .runtime import ToolCancelledError, ToolRuntime

logger = get_logger(__name__)

def _capabilities(runtime: ToolRuntime | None):
    capabilities = runtime.capabilities if runtime is not None else None
    if capabilities is None or capabilities.background_tasks is None:
        raise RuntimeError("command tool requires execution and background-task capabilities")
    if runtime is None or runtime.execution is None:
        raise RuntimeError("command tool requires an invocation authorization")
    return capabilities


def _make_background_task(
    task_id: str,
    proc: ProcessHandle,
    output_lines: list[str],
    done_event: threading.Event,
    output_lock: threading.RLock,
    reader_thread: threading.Thread,
    *,
    command: str,
    root_turn_id: str,
    run_id: str = "",
    on_done: Callable[[], None] | None = None,
):
    # 延迟导入避免 session -> tools.base -> tools.__init__ -> command_tools 的环。
    from ...domain.model.session import BackgroundTask

    task = BackgroundTask(
        task_id=task_id,
        on_done=on_done,
        command=command,
        root_turn_id=root_turn_id,
        run_id=run_id,
    )
    if done_event.is_set():
        task.ended_at = time.time()
    return task, ProcessResources(
        proc, output_lines, done_event, output_lock, reader_thread
    )


# ── execute_command ───────────────────────────────────────────────────────────

MAX_OUTPUT_CHARS = 8000


def execute_command(
    command: str,
    timeout: int = 20,
    run_in_background: bool = False,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    """
    执行 shell 命令，对齐 Claude Code BashTool 的核心机制：

    - cwd 注入法：在命令末尾追加 `&& pwd -P > $tmpfile`，命令执行完后
      读回临时文件来更新 session cwd，能正确捕获命令内部 cd 的效果。
    - 流式输出：后台读线程实时触发 runtime 里的输出回调。
    - 超时转后台：前台命令超时后不 kill，转为后台任务返回 task_id。
    - run_in_background：立即后台运行，返回 task_id。
    """
    try:
        capabilities = _capabilities(runtime)

        def result_data(
            payload: dict | None = None,
            *,
            cwd: ExecutionPath | None = None,
        ) -> dict:
            data = dict(payload or {})
            data["cwd"] = _format_cwd(runtime, cwd=cwd)
            return data

        if (
            run_in_background
            and runtime is not None
            and not runtime.allow_background_tasks
        ):
            return ToolResult.fail(
                "This Agent cannot create background tasks",
                data=result_data(),
            )
        # The tool owns shell-specific cwd tracking and output semantics; the
        # execution backend owns where/how the approved process is created.
        proc = runtime.execution.start_shell(command)
    except FileNotFoundError:
        return ToolResult.fail(
            f"command not found: {command.split()[0] if command.split() else command}"
        )
    except Exception as e:
        return ToolResult.fail(f"{type(e).__name__}: {e}")

    output_lines: list[str] = []
    output_lock = threading.RLock()
    done_event = threading.Event()
    cwd_result: list[ExecutionPath] = []

    # 后台 task 可能在 reader 启动后才创建（前台 timeout 转后台）。holder
    # 让 reader 在完成时补 ended_at 和通知；极短命令先结束时，注册路径会补发。
    background_holder: list[Any | None] = [None]
    notification_lock = threading.Lock()
    notification_sent = False

    def _notify_background_done() -> None:
        nonlocal notification_sent
        task = background_holder[0]
        if task is None or task.on_done is None:
            return
        with notification_lock:
            if notification_sent:
                return
            notification_sent = True
        try:
            task.on_done()
        except Exception:
            logger.debug("background command on_done callback failed", exc_info=True)

    def _reader():
        for line in proc.iter_output():
            with output_lock:
                output_lines.append(line)
            if runtime and runtime.emit_output:
                runtime.emit_output(line)
        proc.wait()
        # reader 只采集命令结束时的 cwd，不负责提交。只有前台调用路径确认命令
        # 没有转后台后才会更新 session，彻底消除 timeout 临界点的提交竞态。
        new_cwd = proc.cwd_result()
        if new_cwd is not None:
            cwd_result.append(new_cwd)
        background = background_holder[0]
        if background is not None:
            background.ended_at = time.time()
        done_event.set()
        _notify_background_done()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    if run_in_background:
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        notify = runtime.notify_background_done if runtime else None
        background, resources = _make_background_task(
            task_id, proc, output_lines, done_event, output_lock, reader_thread,
            command=command,
            root_turn_id=capabilities.scope.root_turn_id,
            run_id=capabilities.scope.run_id,
            on_done=(lambda: notify(task_id)) if notify is not None else None,
        )
        background_holder[0] = background
        if runtime is None or runtime.runtime_resources is None:
            raise RuntimeError("background command requires RuntimeResources")
        capabilities.background_tasks.register(background)
        runtime.runtime_resources.process_registry.register(task_id, resources)
        if done_event.is_set():
            _notify_background_done()
        return ToolResult.success(result_data({
            "task_id": task_id,
            "message": f"Command is running in the background; use get_task for {task_id}.",
        }))

    deadline = time.monotonic() + timeout
    finished = False
    while not finished:
        if runtime and runtime.is_cancelled():
            proc.terminate()
            done_event.wait(timeout=2)
            raise ToolCancelledError("execute_command cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        finished = done_event.wait(timeout=min(0.1, remaining))

    if not finished:
        if runtime is not None and not runtime.allow_background_tasks:
            proc.terminate()
            done_event.wait(timeout=2)
            with output_lock:
                output_so_far = "".join(output_lines)[-MAX_OUTPUT_CHARS:]
            return ToolResult.fail(
                f"Command exceeded {timeout}s; this Agent cannot convert it to a background task",
                data=result_data({"timed_out": True, "output_so_far": output_so_far}),
            )
        # 超时：不 kill，转后台
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        notify = runtime.notify_background_done if runtime else None
        background, resources = _make_background_task(
            task_id, proc, output_lines, done_event, output_lock, reader_thread,
            command=command,
            root_turn_id=capabilities.scope.root_turn_id,
            run_id=capabilities.scope.run_id,
            on_done=(lambda: notify(task_id)) if notify is not None else None,
        )
        background_holder[0] = background
        if runtime is None or runtime.runtime_resources is None:
            raise RuntimeError("background command requires RuntimeResources")
        capabilities.background_tasks.register(background)
        runtime.runtime_resources.process_registry.register(task_id, resources)
        if done_event.is_set():
            _notify_background_done()
        with output_lock:
            output_so_far = "".join(output_lines)[-MAX_OUTPUT_CHARS:]
        return ToolResult.success(result_data({
            "task_id": task_id,
            "timed_out": True,
            "message": f"Command exceeded {timeout}s; moved to background task {task_id}.",
            "output_so_far": output_so_far,
        }))

    current_cwd = cwd_result[0] if cwd_result else runtime.execution.cwd()
    if cwd_result and capabilities.set_cwd is not None:
        capabilities.set_cwd(current_cwd)

    reset_cwd = False
    scope = runtime.access_scope
    if scope is not None and not scope.contains(current_cwd.value):
        origin = ExecutionPath(current_cwd.environment_id, str(scope.origin))
        if capabilities.set_cwd is not None:
            capabilities.set_cwd(origin)
            reset_cwd = True
        current_cwd = origin

    with output_lock:
        output = "".join(output_lines)
    if len(output) > MAX_OUTPUT_CHARS:
        output = f"[...truncated, showing tail]\n{output[-MAX_OUTPUT_CHARS:]}"

    returncode = proc.returncode
    payload = {"returncode": returncode, "output": output}
    if reset_cwd:
        payload["cwd_reset"] = True
        payload["message"] = (
            f"Shell cwd was reset to "
            f"{scope.origin if scope is not None else current_cwd.value}"
        )
    data = result_data(payload, cwd=current_cwd)

    if returncode == 0:
        return ToolResult.success(data)
    return ToolResult.fail(err=f"Command exited with code {returncode}", data=data)


def _format_cwd(
    runtime: ToolRuntime | None,
    *,
    cwd: ExecutionPath | None = None,
) -> str:
    if runtime is None or runtime.execution is None:
        return "."
    return runtime.execution.display_path(cwd or runtime.execution.cwd())


# ── 工具定义 ──────────────────────────────────────────────────────────────────

execute_command_tool = Tool(
    name="execute_command",
    description=(
        "Execute a shell command in the workspace. "
        "The working directory persists across calls (cd works) and is returned as cwd "
        "on every result — do not cd into the directory you are already in. "
        "Long-running commands auto-background after timeout and return a task_id. "
        "Set run_in_background=true to background immediately."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds before auto-backgrounding (default: 20)",
                "default": 20,
            },
            "run_in_background": {
                "type": "boolean",
                "description": "If true, run immediately in background and return task_id",
                "default": False,
            },
        },
        "required": ["command"],
    },
    call=lambda args, runtime: execute_command(**args, runtime=runtime),
    access_descriptor=describe_execute_command_access,
    is_concurrency_safe=is_execute_command_concurrency_safe,
    required_capabilities=frozenset({"execution", "cwd", "background"}),
    # shell 自己负责前台 timeout → 后台 task 的语义。
    timeout_owner="tool",
)
