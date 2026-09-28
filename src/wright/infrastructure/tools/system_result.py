"""Attach a display code to a model-facing tool failure without translating it."""

from __future__ import annotations

from ...domain.feedback import SystemText
from ...domain.model.tool import ToolResult


def fail_text(error: SystemText | str, *, data=None) -> ToolResult:
    if isinstance(error, SystemText):
        payload = dict(data or {})
        payload["display_code"] = error.code
        payload["display_params"] = error.param_dict()
        return ToolResult.fail(error.message, payload)
    return ToolResult.fail(error, data)


__all__ = ["fail_text"]
