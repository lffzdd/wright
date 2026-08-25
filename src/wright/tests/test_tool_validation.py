from ..executor import ToolExecutor
from ..permission import PermissionCheckResult, PermissionResolver
from ..tools.base import Tool, ToolCall, ToolResult


def _schema_tool(calls, permission_calls=None):
    def check_permission(args, runtime):
        if permission_calls is not None:
            permission_calls.append(dict(args))
        return PermissionCheckResult("allow", "test tool policy")

    return Tool(
        name="structured",
        description="",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 2},
                "mode": {"enum": ["read", "write"]},
                "items": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                },
            },
            "required": ["name", "mode"],
            "additionalProperties": False,
        },
        call=lambda args, runtime: (
            calls.append(args) or ToolResult.success(args)
        ),
        check_permission=check_permission,
    )


def test_invalid_arguments_fail_before_permission_and_tool(tmp_path):
    tool_calls = []
    permission_calls = []

    def approval_handler(request):
        permission_calls.append(request)
        return PermissionCheckResult("allow", "test")

    tool = _schema_tool(tool_calls, permission_calls)
    executor = ToolExecutor(
        {tool.name: tool},
        workspace_dir=tmp_path,
        permission_resolver=PermissionResolver(approval_handler=approval_handler),
    )

    outcome = executor.execute([
        ToolCall(
            "structured",
            {"name": "x", "mode": "delete", "items": [1, "two"], "extra": True},
            "c1",
        )
    ])[0]

    assert outcome.status == "failed"
    assert outcome.result.data["error"]["type"] == "tool_input_validation"
    assert len(outcome.result.data["error"]["issues"]) == 4
    assert "InputValidationError" in outcome.result.err
    assert permission_calls == []
    assert tool_calls == []


def test_missing_required_argument_returns_repairable_result(tmp_path):
    tool = _schema_tool([])
    outcome = ToolExecutor(
        {tool.name: tool}, workspace_dir=tmp_path
    ).execute([ToolCall("structured", {"name": "ok"}, "c1")])[0]

    issue = outcome.result.data["error"]["issues"][0]
    assert issue["validator"] == "required"
    assert issue["path"] == "$"
    assert "mode" in issue["message"]


def test_valid_arguments_reach_permission_then_tool(tmp_path):
    calls = []
    tool = _schema_tool(calls)
    outcome = ToolExecutor(
        {tool.name: tool}, workspace_dir=tmp_path
    ).execute([
        ToolCall(
            "structured",
            {"name": "report", "mode": "read", "items": [1, 2]},
            "c1",
        )
    ])[0]

    assert outcome.status == "succeeded"
    assert calls == [{"name": "report", "mode": "read", "items": [1, 2]}]


def test_invalid_tool_schema_is_a_configuration_error(tmp_path):
    tool = Tool(
        "broken",
        "",
        {"type": "definitely-not-a-json-schema-type"},
        lambda args, runtime: ToolResult.success(),
    )
    outcome = ToolExecutor(
        {tool.name: tool}, workspace_dir=tmp_path
    ).execute([ToolCall("broken", {}, "c1")])[0]

    assert outcome.status == "failed"
    assert outcome.result.data["error"]["type"] == "tool_schema_error"
    assert "ToolSchemaError" in outcome.result.err
