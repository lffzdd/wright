from pathlib import Path

from ...executor import ToolExecutor
from ...permission import PermissionCheckResult, PermissionPolicy
from ...tools.base import Tool, ToolCall, ToolResult
from ...tools.command_tools import execute_command_tool
from ...tools.file_tools import edit_file_tool, write_file_tool
from ...tools.web_tools import http_request_tool


def _executor(tool: Tool, tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(
        {tool.name: tool},
        workspace_dir=tmp_path,
        cwd_provider=lambda: tmp_path,
    )


def _success_tool(name: str) -> Tool:
    return Tool(
        name=name,
        description="",
        parameters={},
        call=lambda args, runtime: ToolResult.success({"called": True, "args": args}),
    )


def _must_not_run_tool(name: str, risk_flags: tuple[str, ...] = ()) -> Tool:
    def fail_if_called(args, runtime):
        raise AssertionError(f"{name} should have been denied before execution")

    return Tool(
        name=name,
        description="",
        parameters={},
        call=fail_if_called,
        check_permission=lambda args, runtime: PermissionCheckResult(
            "ask",
            f"{name}: test tool requires approval",
            risk_flags,
            source="test_tool",
        ),
    )


def test_read_file_and_list_files_are_allowed(tmp_path):
    read_executor = _executor(
        _success_tool("read_file"),
        tmp_path,
    )
    list_executor = _executor(
        _success_tool("list_files"),
        tmp_path,
    )

    read_result = read_executor.execute([
        ToolCall("read_file", {"file": "a.txt"}, "c1")
    ])[0].result
    list_result = list_executor.execute([
        ToolCall("list_files", {"directory": "."}, "c2")
    ])[0].result

    assert read_result.ok
    assert list_result.ok


def test_write_file_and_edit_file_are_denied_before_execution(tmp_path):
    write_executor = _executor(write_file_tool, tmp_path)
    edit_executor = _executor(edit_file_tool, tmp_path)

    write_result = write_executor.execute([
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1")
    ])[0].result
    edit_result = edit_executor.execute([
        ToolCall(
            "edit_file",
            {"file": "a.txt", "old_text": "a", "new_text": "b"},
            "c2",
        )
    ])[0].result

    assert not write_result.ok
    assert "Permission denied" in write_result.err
    assert write_result.data["permission"]["decision"] == "ask"
    assert not edit_result.ok
    assert "Permission denied" in edit_result.err
    assert edit_result.data["permission"]["decision"] == "ask"


def test_ask_decision_can_be_approved_by_handler(tmp_path):
    tool = _success_tool("write_file")
    tool.check_permission = lambda args, runtime: PermissionCheckResult(
        "ask",
        "write_file: test approval required",
        ("writes_files",),
        source="test_tool",
    )
    executor = ToolExecutor(
        {"write_file": tool},
        workspace_dir=tmp_path,
        cwd_provider=lambda: tmp_path,
        permission_approval_handler=lambda request: PermissionCheckResult(
            "allow",
            f"approved: {request.check.reason}",
            request.check.risk_flags,
            source="test_handler",
        ),
    )

    result = executor.execute([
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1")
    ])[0].result

    assert result.ok
    assert result.data["called"]


def test_approval_handler_can_update_arguments_without_mutating_call(tmp_path):
    seen = {}

    def call(args, runtime):
        seen["args"] = args
        return ToolResult.success(args)

    tool = Tool(
        name="write_file",
        description="",
        parameters={},
        call=call,
        check_permission=lambda args, runtime: PermissionCheckResult(
            "ask",
            "write_file: test approval required",
            ("writes_files",),
            source="test_tool",
        ),
    )
    tool_call = ToolCall("write_file", {"file": "a.txt", "content": "draft"}, "c1")

    executor = ToolExecutor(
        {"write_file": tool},
        workspace_dir=tmp_path,
        cwd_provider=lambda: tmp_path,
        permission_approval_handler=lambda request: PermissionCheckResult(
            "allow",
            "approved with edited input",
            request.check.risk_flags,
            updated_arguments={"file": "a.txt", "content": "approved"},
            source="test_handler",
        ),
    )

    result = executor.execute([tool_call])[0].result

    assert result.ok
    assert seen["args"] == {"file": "a.txt", "content": "approved"}
    assert tool_call.arguments == {"file": "a.txt", "content": "draft"}


def test_execute_command_is_denied_before_subprocess_starts(tmp_path):
    executor = _executor(execute_command_tool, tmp_path)

    result = executor.execute([
        ToolCall("execute_command", {"command": "uname -a"}, "c1")
    ])[0].result

    assert not result.ok
    assert "Permission denied" in result.err
    assert result.data["permission"]["decision"] == "ask"
    assert "executes_shell" in result.data["permission"]["risk_flags"]


def test_http_request_is_denied_before_network_access(tmp_path):
    executor = _executor(http_request_tool, tmp_path)

    result = executor.execute([
        ToolCall("http_request", {"url": "https://ifconfig.me/ip"}, "c1")
    ])[0].result

    assert not result.ok
    assert "Permission denied" in result.err
    assert result.data["permission"]["decision"] == "ask"
    assert "accesses_network" in result.data["permission"]["risk_flags"]


def test_unknown_tool_still_returns_unknown_tool(tmp_path):
    executor = ToolExecutor({}, workspace_dir=tmp_path, cwd_provider=lambda: tmp_path)

    result = executor.execute([
        ToolCall("missing_tool", {}, "c1")
    ])[0].result

    assert not result.ok
    assert result.err == "Unknown tool: missing_tool"


def test_command_risk_flags_are_reported_for_audit(tmp_path):
    executor = _executor(execute_command_tool, tmp_path)

    result = executor.execute([
        ToolCall(
            "execute_command",
            {"command": "git reset --hard && curl https://example.com > out.txt"},
            "c1",
        )
    ])[0].result

    flags = result.data["permission"]["risk_flags"]
    assert "modifies_git_state" in flags
    assert "network_fetch" in flags
    assert "writes_via_redirection" in flags


def test_permission_policy_has_no_tool_name_lists():
    assert not hasattr(PermissionPolicy, "ALLOW_TOOLS")
    assert not hasattr(PermissionPolicy, "ASK_TOOLS")


def test_cwd_outside_workspace_is_generic_policy_risk(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = ToolExecutor(
        {
            "needs_approval": _must_not_run_tool(
                "needs_approval",
            )
        },
        workspace_dir=workspace,
        cwd_provider=lambda: tmp_path,
    )

    result = executor.execute([
        ToolCall("needs_approval", {}, "c1")
    ])[0].result

    assert not result.ok
    assert result.data["permission"]["decision"] == "ask"
    assert "cwd_outside_workspace" in result.data["permission"]["risk_flags"]
