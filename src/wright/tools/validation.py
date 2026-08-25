"""Tool argument validation at the execution boundary.

The model-visible ``Tool.parameters`` value is JSON Schema.  Advertising that
schema without enforcing it leaves permission checks and tool functions to
receive malformed values.  This module turns schema failures into ordinary,
structured tool results so the model can repair its next call.
"""

from __future__ import annotations

from typing import Any

from jsonschema import SchemaError, ValidationError, validators

from .base import Tool, ToolResult


def validate_tool_arguments(tool: Tool, arguments: dict[str, Any]) -> ToolResult | None:
    """Return ``None`` when valid, otherwise a structured failed result.

    Schema validation deliberately happens before permission resolution: an
    invalid call must neither prompt the user nor reach tool-specific policy.
    """
    try:
        validator_cls = validators.validator_for(tool.parameters)
        validator_cls.check_schema(tool.parameters)
        errors = sorted(
            validator_cls(tool.parameters).iter_errors(arguments),
            key=_error_sort_key,
        )
    except SchemaError as exc:
        return _configuration_error(tool, exc)
    except Exception as exc:
        # Invalid/unresolvable references and custom schema failures are tool
        # configuration errors, not model input mistakes.
        return ToolResult.fail(
            f"ToolSchemaError: {tool.name} 的参数 schema 无法使用: {exc}",
            data={
                "error": {
                    "type": "tool_schema_error",
                    "tool": tool.name,
                    "message": str(exc),
                }
            },
        )

    if not errors:
        return None

    details = [_validation_detail(error) for error in errors]
    first = details[0]
    location = first["path"] or "$"
    return ToolResult.fail(
        f"InputValidationError: {tool.name} 参数 {location} {first['message']}",
        data={
            "error": {
                "type": "tool_input_validation",
                "tool": tool.name,
                "issues": details,
            }
        },
    )


def _configuration_error(tool: Tool, error: SchemaError) -> ToolResult:
    detail = _validation_detail(error)
    return ToolResult.fail(
        f"ToolSchemaError: {tool.name} 的参数 schema 非法: {detail['message']}",
        data={
            "error": {
                "type": "tool_schema_error",
                "tool": tool.name,
                "issues": [detail],
            }
        },
    )


def _validation_detail(error: ValidationError | SchemaError) -> dict[str, Any]:
    return {
        "path": _json_path(error.absolute_path),
        "schema_path": _json_path(error.absolute_schema_path),
        "validator": error.validator,
        "message": error.message,
    }


def _json_path(parts) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            escaped = str(part).replace("~", "~0").replace("/", "~1")
            path += f"/{escaped}"
    return path


def _error_sort_key(error: ValidationError) -> tuple[str, str]:
    return (_json_path(error.absolute_path), error.message)
