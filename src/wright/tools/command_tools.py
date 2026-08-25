import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..processes import terminate_process_tree
from .base import Tool, ToolCancelledError, ToolResult, ToolRuntime
from .command_permissions import (
    check_execute_command_permission,
    is_execute_command_concurrency_safe,
)

def _session(runtime: ToolRuntime | None) -> Any:
    session = runtime.session_state if runtime is not None else None
    if session is None or not hasattr(session, "get_cwd"):
        raise RuntimeError("command tool requires a SessionState runtime")
    return session


def _make_background_task(
    task_id: str,
    proc: subprocess.Popen,
    output_lines: list[str],
    done_event: threading.Event,
    output_lock: threading.RLock,
    *,
    command: str,
    root_turn_id: str,
    on_done: Callable[[], None] | None = None,
):
    # 延迟导入避免 session -> tools.base -> tools.__init__ -> command_tools 的环。
    from ..session import BackgroundTask

    task = BackgroundTask(
        task_id=task_id,
        process=proc,
        output_lines=output_lines,
        done=done_event,
        output_lock=output_lock,
        on_done=on_done,
        command=command,
        root_turn_id=root_turn_id,
    )
    if done_event.is_set():
        task.ended_at = time.time()
    return task


def get_task_output(task_id: str, runtime: ToolRuntime | None = None) -> ToolResult:
    """Compatibility adapter for the old shell-only task query."""
    try:
        from ..tasks import TaskNotFoundError, TaskService

        task = TaskService.for_session(_session(runtime)).get(task_id)
        if task.kind != "shell":
            return ToolResult.fail(f"Task is not a shell task: {task_id}")
    except TaskNotFoundError as e:
        return ToolResult.fail(str(e))
    except Exception as e:
        return ToolResult.fail(str(e))
    return ToolResult.success(
        {
            "task_id": task_id,
            "done": task.terminal,
            "returncode": task.returncode,
            "output": task.output,
        }
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
        session = _session(runtime)
        if (
            run_in_background
            and runtime is not None
            and not runtime.allow_background_tasks
        ):
            return ToolResult.fail("当前 Agent 不允许创建后台任务")
        cwd = session.get_cwd()

        # 注入 cwd 追踪：用临时文件，和 Claude Code 的 claude-{id}-cwd 一致
        tmp = tempfile.NamedTemporaryFile(prefix="wright-cwd-", delete=False)
        cwd_file = Path(tmp.name)
        tmp.close()
        cwd_file.unlink(missing_ok=True)
        # 末尾追加 `&& pwd -P > tmpfile`，无论命令成败都不影响返回码
        # （pwd -P 只在主命令成功时才写，和 Claude Code 的 &&  行为一致）
        injected = (
            f"eval {shlex.quote(command)} && pwd -P > {shlex.quote(str(cwd_file))}"
        )

        proc = subprocess.Popen(
            ["/bin/bash", "-c", injected],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # stderr 合并进 stdout，和 Claude Code 一致
            text=True,
            bufsize=1,
            # A task owns the whole command tree, so cancel_task can terminate
            # descendants instead of orphaning them.
            start_new_session=True,
        )
    except FileNotFoundError:
        return ToolResult.fail(
            f"命令不存在: {command.split()[0] if command.split() else command}"
        )
    except Exception as e:
        return ToolResult.fail(f"{type(e).__name__}: {e}")

    output_lines: list[str] = []
    output_lock = threading.RLock()
    done_event = threading.Event()
    cwd_result: list[Path] = []

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
            pass

    def _reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            with output_lock:
                output_lines.append(line)
            if runtime and runtime.emit_output:
                runtime.emit_output(line)
        proc.wait()
        # reader 只采集命令结束时的 cwd，不负责提交。只有前台调用路径确认命令
        # 没有转后台后才会更新 session，彻底消除 timeout 临界点的提交竞态。
        new_cwd = _consume_cwd_file(cwd_file)
        if new_cwd is not None:
            cwd_result.append(new_cwd)
        background = background_holder[0]
        if background is not None:
            background.ended_at = time.time()
        done_event.set()
        _notify_background_done()

    threading.Thread(target=_reader, daemon=True).start()

    if run_in_background:
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        notify = runtime.notify_background_done if runtime else None
        background = _make_background_task(
            task_id, proc, output_lines, done_event, output_lock,
            command=command,
            root_turn_id=session.agent_root_turn_id,
            on_done=(lambda: notify(task_id)) if notify is not None else None,
        )
        background_holder[0] = background
        session.register_background_task(background)
        if done_event.is_set():
            _notify_background_done()
        return ToolResult.success(
            {
                "task_id": task_id,
                "message": f"命令已在后台运行，用 get_task 查询 {task_id}。",
            }
        )

    deadline = time.monotonic() + timeout
    finished = False
    while not finished:
        if runtime and runtime.is_cancelled():
            terminate_process_tree(proc)
            done_event.wait(timeout=2)
            raise ToolCancelledError("execute_command cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        finished = done_event.wait(timeout=min(0.1, remaining))

    if not finished:
        if runtime is not None and not runtime.allow_background_tasks:
            terminate_process_tree(proc)
            done_event.wait(timeout=2)
            with output_lock:
                output_so_far = "".join(output_lines)[-MAX_OUTPUT_CHARS:]
            return ToolResult.fail(
                f"命令超过 {timeout}s；当前 Agent 禁止转为后台任务",
                data={"timed_out": True, "output_so_far": output_so_far},
            )
        # 超时：不 kill，转后台
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        notify = runtime.notify_background_done if runtime else None
        background = _make_background_task(
            task_id, proc, output_lines, done_event, output_lock,
            command=command,
            root_turn_id=session.agent_root_turn_id,
            on_done=(lambda: notify(task_id)) if notify is not None else None,
        )
        background_holder[0] = background
        session.register_background_task(background)
        if done_event.is_set():
            _notify_background_done()
        with output_lock:
            output_so_far = "".join(output_lines)[-MAX_OUTPUT_CHARS:]
        return ToolResult.success(
            {
                "task_id": task_id,
                "timed_out": True,
                "message": f"命令超过 {timeout}s 未完成，已转为后台任务 {task_id}。",
                "output_so_far": output_so_far,
            }
        )

    if cwd_result:
        session.set_cwd(cwd_result[0])

    with output_lock:
        output = "".join(output_lines)
    if len(output) > MAX_OUTPUT_CHARS:
        output = f"[...截断，仅显示末尾]\n{output[-MAX_OUTPUT_CHARS:]}"

    returncode = proc.returncode
    data = {"returncode": returncode, "output": output}

    if returncode == 0:
        return ToolResult.success(data)
    return ToolResult.fail(err=f"命令以退出码 {returncode} 结束", data=data)


def _consume_cwd_file(cwd_file: Path) -> Path | None:
    """读取并删除 pwd -P 写入的临时文件，返回可用 cwd 候选值。

    和 Claude Code Shell.ts 里的 readFileSync + unlinkSync 逻辑对应。
    是否提交给 session 由前台调用路径决定；后台 reader 永远不能直接改 cwd。
    """
    try:
        new_cwd = Path(cwd_file.read_text().strip())
        return new_cwd if new_cwd.is_dir() else None
    except Exception:
        return None  # 文件不存在（命令失败）或路径非法，静默忽略
    finally:
        try:
            cwd_file.unlink()
        except Exception:
            pass


# ── 工具定义 ──────────────────────────────────────────────────────────────────

execute_command_tool = Tool(
    name="execute_command",
    description=(
        "Execute a shell command in the workspace. "
        "The working directory persists across calls (cd works). "
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
    check_permission=check_execute_command_permission,
    is_concurrency_safe=is_execute_command_concurrency_safe,
    # shell 自己负责前台 timeout → 后台 task 的语义。
    timeout_owner="tool",
)

get_task_output_tool = Tool(
    name="get_task_output",
    description=(
        "Compatibility alias for shell tasks. Prefer get_task, which also supports "
        "Agent tasks and returns the unified lifecycle model."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task_id returned by execute_command",
            },
        },
        "required": ["task_id"],
    },
    call=lambda args, runtime: get_task_output(**args, runtime=runtime),
    is_concurrency_safe=lambda args: True,
    expose_to_model=False,
)
