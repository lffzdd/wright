from pathlib import Path

from ...engine.executor import ToolExecutor
from ...permission import (
    AccessTarget,
    PermissionPolicy,
    PermissionResponse,
    ToolAccess,
)
from ...tools.base import Tool, ToolCall, ToolResult
from ...tools.command_tools import execute_command_tool
from ...tools.file_tools import (
    edit_file_tool,
    list_directory_tool,
    read_file_tool,
    write_file_tool,
)
from ...tools.web_tools import http_request_tool


def _executor(tool: Tool, tmp_path: Path, **kwargs) -> ToolExecutor:
    return ToolExecutor(
        {tool.name: tool},
        workspace_dir=tmp_path,
        cwd_provider=lambda: tmp_path,
        **kwargs,
    )


def _success_tool(name: str, access: ToolAccess | None = None) -> Tool:
    return Tool(
        name=name,
        description="",
        parameters={},
        call=lambda args, runtime: ToolResult.success({"called": True, "args": args}),
        access_descriptor=lambda args: access or ToolAccess.internal_read(),
    )


def _approval(choice="allow_once", updated_arguments=None):
    return lambda request: PermissionResponse(choice, updated_arguments)


def test_read_only_internal_tools_are_allowed_by_default(tmp_path):
    tool = _success_tool("read_state")
    result = _executor(tool, tmp_path).execute([
        ToolCall("read_state", {}, "c1")
    ])[0].result
    assert result.ok


def test_undeclared_tool_access_is_not_trusted(tmp_path):
    tool = Tool(
        "legacy", "legacy", {"type": "object"},
        lambda _args, _runtime: ToolResult.success(),
    )
    result = _executor(tool, tmp_path).execute([
        ToolCall("legacy", {}, "c1")
    ])[0].result
    assert not result.ok
    assert "未声明" in result.err


def test_read_file_and_list_directory_are_allowed_by_default(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    read_result = _executor(read_file_tool, tmp_path).execute([
        ToolCall("read_file", {"file": "a.txt"}, "c1")
    ])[0].result
    list_result = _executor(list_directory_tool, tmp_path).execute([
        ToolCall("list_directory", {"directory": "."}, "c2")
    ])[0].result
    assert read_result.ok and list_result.ok


def test_write_file_and_edit_file_are_denied_without_interaction(tmp_path):
    write_result = _executor(write_file_tool, tmp_path).execute([
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1")
    ])[0].result
    edit_result = _executor(edit_file_tool, tmp_path).execute([
        ToolCall(
            "edit_file", {"file": "a.txt", "old_text": "a", "new_text": "b"}, "c2"
        )
    ])[0].result
    assert not write_result.ok and write_result.data["permission"]["decision"] == "deny"
    assert not edit_result.ok and edit_result.data["permission"]["decision"] == "deny"


def test_approval_handler_can_allow_and_update_non_permission_arguments(tmp_path):
    seen = {}

    def call(args, runtime):
        seen.update(args)
        return ToolResult.success(args)

    tool = Tool(
        "write_file", "", {}, call,
        access_descriptor=lambda args: ToolAccess(
            frozenset({"file_write"}),
            (AccessTarget("file", str(args.get("file", "a.txt")), "file_write", kind="file"),),
            subject=str(args.get("file", "a.txt")),
        ),
    )
    result = _executor(
        tool,
        tmp_path,
        permission_approval_handler=_approval(
            updated_arguments={"file": "a.txt", "content": "approved"}
        ),
    ).execute([ToolCall("write_file", {"file": "a.txt", "content": "draft"}, "c1")])[0].result
    assert result.ok
    assert seen == {"file": "a.txt", "content": "approved"}


def test_permission_rewrite_rechecks_new_target(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    tool = Tool(
        "read_custom", "", {}, lambda args, runtime: ToolResult.success(args),
        access_descriptor=lambda args: ToolAccess(
            frozenset({"file_write"}),
            (AccessTarget("file", str(args["file"]), "file_write", kind="file"),),
            subject=str(args["file"]),
        ),
    )
    calls = 0
    def approval(request):
        nonlocal calls
        calls += 1
        return PermissionResponse("allow_once" if calls == 1 else "deny", {"file": str(outside)})
    result = _executor(
        tool,
        tmp_path,
        permission_approval_handler=approval,
    ).execute([ToolCall("read_custom", {"file": "inside.txt"}, "c1")])[0].result
    assert not result.ok
    assert result.data["permission"]["decision"] == "deny"


def test_second_permission_target_rewrite_is_rejected(tmp_path):
    calls = 0

    def approval(request):
        nonlocal calls
        calls += 1
        return PermissionResponse(
            "allow_once",
            {"file": str(tmp_path.parent / f"outside-{calls}.txt")},
        )

    tool = Tool(
        "read_custom", "", {}, lambda args, runtime: ToolResult.success(args),
        access_descriptor=lambda args: ToolAccess(
            frozenset({"file_write"}),
            (AccessTarget("file", str(args["file"]), "file_write", kind="file"),),
            subject=str(args["file"]),
        ),
    )
    result = _executor(
        tool, tmp_path, permission_approval_handler=approval
    ).execute([ToolCall("read_custom", {"file": "inside.txt"}, "c1")])[0].result
    assert not result.ok
    assert result.data["permission"]["source"] == "rewrite_loop"
    assert calls == 2


def test_execute_command_is_denied_before_subprocess_starts(tmp_path):
    result = _executor(execute_command_tool, tmp_path).execute([
        ToolCall("execute_command", {"command": "uname -a"}, "c1")
    ])[0].result
    assert not result.ok
    assert "executes_shell" in result.data["permission"]["risk_flags"]


def test_http_request_is_denied_before_network_access(tmp_path):
    result = _executor(http_request_tool, tmp_path).execute([
        ToolCall("http_request", {"url": "https://ifconfig.me/ip"}, "c1")
    ])[0].result
    assert not result.ok
    assert result.data["permission"]["risk_flags"] == ["network_read"]


def test_command_risk_flags_are_reported_for_audit(tmp_path):
    result = _executor(execute_command_tool, tmp_path).execute([
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


def test_cwd_outside_workspace_is_not_a_global_deny(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _success_tool(
        "needs_approval",
        ToolAccess(frozenset({"network_write"}), risk_flags=("network_write",)),
    )
    result = ToolExecutor(
        {tool.name: tool}, workspace_dir=workspace, cwd_provider=lambda: tmp_path
    ).execute([ToolCall(tool.name, {}, "c1")])[0].result
    assert not result.ok
    assert "cwd_outside_workspace" not in result.data["permission"]["risk_flags"]


def test_missing_tool_remains_an_unknown_tool_error(tmp_path):
    result = ToolExecutor({}, workspace_dir=tmp_path).execute([
        ToolCall("missing_tool", {}, "c1")
    ])[0].result
    assert not result.ok
    assert result.err == "Unknown tool: missing_tool"
