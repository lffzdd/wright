"""Human-in-the-loop：声明问题；交互层回填答案后再生成工具结果。"""

from __future__ import annotations

from ..permission import PermissionCheckResult
from .base import Tool, ToolResult, ToolRuntime


MAX_QUESTION_LENGTH = 1_000
MAX_CONTEXT_LENGTH = 1_000
MAX_OPTIONS = 8
MAX_OPTION_LENGTH = 200


def _clean_text(value: object, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ValueError(f"{field} 不能超过 {max_length} 个字符")
    return cleaned


def _clean_options(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        raise ValueError("options 必须是字符串数组")
    if len(values) > MAX_OPTIONS:
        raise ValueError(f"options 不能超过 {MAX_OPTIONS} 项")

    options = tuple(
        _clean_text(value, f"options[{idx}]", MAX_OPTION_LENGTH)
        for idx, value in enumerate(values)
    )
    if len(set(options)) != len(options):
        raise ValueError("options 不能包含重复项")
    return options


def ask_user(
    question: str,
    context: str = "",
    options: list[str] | None = None,
    answer: str | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    """消费交互层已回填的回答，生成标准 tool_result。

    这里绝不直接读 stdin 或调用 UI callback。用户交互发生在 PermissionResolver：
    它先把问题交给 interaction_handler，收到 answer 后放进 updated_arguments，
    然后执行器才会调用本函数。这让 Agent 主循环和工具本体都不认识暂停/恢复。
    """
    try:
        question = _clean_text(question, "question", MAX_QUESTION_LENGTH)
        if not isinstance(context, str):
            raise ValueError("context 必须是字符串")
        context = context.strip()
        if len(context) > MAX_CONTEXT_LENGTH:
            raise ValueError(f"context 不能超过 {MAX_CONTEXT_LENGTH} 个字符")
        _clean_options(options)
        answer = _clean_text(answer, "answer", MAX_QUESTION_LENGTH)
        return ToolResult.success(
            {
                "question": question,
                "answer": answer,
            }
        )
    except Exception as e:
        return ToolResult.fail(str(e))


def check_ask_user_permission(
    arguments: dict, runtime: ToolRuntime
) -> PermissionCheckResult:
    """校验提问形状并强制进入统一用户交互层。"""
    try:
        question = _clean_text(arguments.get("question"), "question", MAX_QUESTION_LENGTH)
        context = arguments.get("context", "")
        if not isinstance(context, str):
            raise ValueError("context 必须是字符串")
        if len(context.strip()) > MAX_CONTEXT_LENGTH:
            raise ValueError(f"context 不能超过 {MAX_CONTEXT_LENGTH} 个字符")
        _clean_options(arguments.get("options"))
    except ValueError as e:
        return PermissionCheckResult(
            "deny", f"ask_user 输入无效: {e}", source="tool_validation"
        )

    return PermissionCheckResult(
        "ask",
        f"ask_user 需要用户回答: {question}",
        source="tool_interaction",
    )


ask_user_tool = Tool(
    name="ask_user",
    description=(
        "当缺少的信息会实质改变方案、存在多个不可自行判断的选择，或必须由用户确认时，"
        "向用户提出一个明确问题并等待回答。不要用它询问可以通过现有工具自行查明的信息。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "需要用户回答的单个、具体问题",
            },
            "context": {
                "type": "string",
                "description": "可选：为什么需要询问，以及答案会影响什么",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_OPTIONS,
                "description": "可选的候选答案；用户仍可自由输入其它回答",
            },
        },
        "required": ["question"],
    },
    call=lambda args, runtime: ask_user(**args, runtime=runtime),
    check_permission=check_ask_user_permission,
    requires_user_interaction=True,
)
