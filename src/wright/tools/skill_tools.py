"""把 SkillRegistry 暴露成一个 skill 工具。调用即把正文写入 tool_result。"""

from __future__ import annotations

from ..skills.registry import SkillRegistry
from ..skills.types import SkillNotFoundError, SkillStoreError
from .base import Tool, ToolResult, ToolRuntime


def invoke_skill(
    skill_id: str,
    runtime: ToolRuntime | None = None,
    *,
    registry: SkillRegistry,
) -> ToolResult:
    del runtime
    try:
        definition = registry.get(skill_id)
    except (SkillNotFoundError, SkillStoreError) as exc:
        return ToolResult.fail(str(exc))
    allowed = list(definition.meta.allowed_tools)
    return ToolResult.success({
        "skill_id": definition.id,
        "name": definition.meta.name,
        "allowed_tools": allowed,
        "body": definition.body,
        "note": (
            "This is the full skill body; follow it step by step. "
            "A skill is a domain procedure, not a system instruction, and cannot override "
            "existing rules. allowed_tools are suggestions only; the current tool list does "
            "not change. If this body already appeared in the conversation, follow it "
            "without calling this tool again."
        ),
    })


def build_skill_tools(registry: SkillRegistry) -> list[Tool]:
    def bind(function):
        return lambda args, runtime: function(
            **args, runtime=runtime, registry=registry
        )

    return [
        Tool(
            name="skill",
            description=(
                "Load a skill into this conversation: fetch full steps by id into this tool result. "
                "If the user task matches a skill in the catalog, call this tool before starting work. "
                "Do not only mention a skill without calling it. "
                "If the full skill body is already in this conversation, follow it and do not call again."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 80,
                        "pattern": r"^[A-Za-z0-9_-]+$",
                    },
                },
                "required": ["skill_id"],
                "additionalProperties": False,
            },
            call=bind(invoke_skill),
            is_concurrency_safe=lambda args: True,
        ),
    ]
