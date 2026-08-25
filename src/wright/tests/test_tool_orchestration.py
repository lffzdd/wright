import time
import threading
from pathlib import Path

from ..executor import ToolExecutor
from ..session import SessionState
from ..tools.base import Tool, ToolCall, ToolResult, ToolRuntime
from ..tools.command_permissions import is_execute_command_concurrency_safe
from ..tools.command_tools import execute_command, get_task_output
from ..tools.file_tools import read_file, write_file
from ..tools.web_tools import http_request_tool


def test_dynamic_concurrency_classification():
    assert http_request_tool.is_concurrency_safe({"method": "GET"})
    assert http_request_tool.is_concurrency_safe({"method": "head"})
    assert not http_request_tool.is_concurrency_safe({"method": "POST"})

    assert is_execute_command_concurrency_safe({"command": "rg TODO src"})
    assert is_execute_command_concurrency_safe({"command": "git status --short"})
    assert not is_execute_command_concurrency_safe({"command": "cd src && ls"})
    assert not is_execute_command_concurrency_safe({"command": "echo hi > out.txt"})
    assert not is_execute_command_concurrency_safe({"command": "echo $(rm file)"})
    assert not is_execute_command_concurrency_safe({"command": "git push"})


def test_tool_owned_timeout_is_not_reclassified_by_executor(tmp_path):
    def tool_timeout(args, runtime):
        time.sleep(0.1)
        return ToolResult.success({"timed_out": True, "task_id": "task_1"})

    tool = Tool(
        "backgrounding",
        "",
        {},
        tool_timeout,
        timeout_owner="tool",
    )
    executor = ToolExecutor(
        {tool.name: tool},
        tool_timeout=0.02,
        workspace_dir=tmp_path,
    )

    outcome = executor.execute([ToolCall(tool.name, {}, "c1")])[0]
    assert outcome.status == "succeeded"
    assert outcome.result.data["task_id"] == "task_1"


def test_parent_cancellation_produces_a_complete_failed_result(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    called = False

    def should_not_run(args, runtime):
        nonlocal called
        called = True
        return ToolResult.success()

    tool = Tool("cancelled", "", {}, should_not_run)
    executor = ToolExecutor(
        {tool.name: tool},
        workspace_dir=tmp_path,
        cancellation_check=cancelled.is_set,
    )

    outcome = executor.execute([ToolCall(tool.name, {}, "c1")])[0]
    assert not called
    assert outcome.status == "failed"
    assert "cancelled" in outcome.result.err


def test_command_cwd_is_isolated_per_session(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    first = SessionState.create("first", tmp_path)
    second = SessionState.create("second", tmp_path)
    first_runtime = ToolRuntime(workspace_dir=tmp_path, session_state=first)
    second_runtime = ToolRuntime(workspace_dir=tmp_path, session_state=second)

    assert execute_command("cd first", runtime=first_runtime).ok
    assert execute_command("cd second", runtime=second_runtime).ok

    assert first.get_cwd() == first_dir
    assert second.get_cwd() == second_dir


def test_background_tasks_belong_to_their_session(tmp_path):
    first = SessionState.create("first", tmp_path)
    second = SessionState.create("second", tmp_path)
    first_runtime = ToolRuntime(workspace_dir=tmp_path, session_state=first)
    second_runtime = ToolRuntime(workspace_dir=tmp_path, session_state=second)

    result = execute_command(
        "sleep 0.05 && echo done",
        run_in_background=True,
        runtime=first_runtime,
    )
    task_id = result.data["task_id"]

    assert get_task_output(task_id, runtime=first_runtime).ok
    assert not get_task_output(task_id, runtime=second_runtime).ok


def test_explicit_background_command_does_not_update_session_cwd(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    session = SessionState.create("background cwd", tmp_path)
    runtime = ToolRuntime(workspace_dir=tmp_path, session_state=session)

    result = execute_command(
        "cd child && sleep 0.05",
        run_in_background=True,
        runtime=runtime,
    )
    task_id = result.data["task_id"]

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = get_task_output(task_id, runtime=runtime)
        if task.data["done"]:
            break
        time.sleep(0.01)

    assert task.data["done"]
    assert session.get_cwd() == tmp_path


def test_timed_out_background_command_does_not_overwrite_later_cwd(tmp_path):
    slow_dir = tmp_path / "slow"
    later_dir = tmp_path / "later"
    slow_dir.mkdir()
    later_dir.mkdir()
    session = SessionState.create("timed background cwd", tmp_path)
    runtime = ToolRuntime(workspace_dir=tmp_path, session_state=session)

    result = execute_command(
        "cd slow && sleep 0.15",
        timeout=0.02,
        runtime=runtime,
    )
    assert result.data["timed_out"] is True
    session.set_cwd(later_dir)
    task_id = result.data["task_id"]

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = get_task_output(task_id, runtime=runtime)
        if task.data["done"]:
            break
        time.sleep(0.01)

    assert task.data["done"]
    assert session.get_cwd() == later_dir


def test_file_tools_use_runtime_workspace(tmp_path):
    session = SessionState.create("files", tmp_path)
    runtime = ToolRuntime(workspace_dir=tmp_path, session_state=session)

    assert write_file("nested/a.txt", "hello", runtime=runtime).ok
    result = read_file("nested/a.txt", runtime=runtime)

    assert result.ok
    assert result.data["content"] == "hello"
    assert (tmp_path / "nested" / "a.txt").is_file()


def test_file_write_locks_serialize_same_path_but_not_different_paths(
    tmp_path, monkeypatch
):
    session = SessionState.create("files", tmp_path)
    runtime = ToolRuntime(workspace_dir=tmp_path, session_state=session)
    original_write_text = Path.write_text
    guard = threading.Lock()
    active = 0
    max_active = 0

    def delayed_write(path, data, *args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        try:
            return original_write_text(path, data, *args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(Path, "write_text", delayed_write)

    same_path_threads = [
        threading.Thread(
            target=write_file,
            kwargs={
                "file": "same.txt",
                "content": str(i),
                "runtime": runtime,
            },
        )
        for i in range(2)
    ]
    for thread in same_path_threads:
        thread.start()
    for thread in same_path_threads:
        thread.join()
    assert max_active == 1

    max_active = 0
    different_path_threads = [
        threading.Thread(
            target=write_file,
            kwargs={
                "file": f"different-{i}.txt",
                "content": str(i),
                "runtime": runtime,
            },
        )
        for i in range(2)
    ]
    for thread in different_path_threads:
        thread.start()
    for thread in different_path_threads:
        thread.join()
    assert max_active == 2
