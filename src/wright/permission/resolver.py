from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .types import PermissionCheckResult

if TYPE_CHECKING:
    # 只在类型注解里用到(配合文件首的 from __future__ import annotations 全部惰性求值),
    # 放进 TYPE_CHECKING 就不会在运行时 import tools——否则 permission 包与 tools 包会
    # 形成 import 环(tools/* 要从本包拿 PermissionCheckResult)。
    from ..tools.base import Tool, ToolCall, ToolRuntime


PermissionApprovalHandler = Callable[["PermissionRequest"], "PermissionCheckResult"]
# 与普通安全权限确认采用同一份 request/result 契约，但语义不同：交互 handler
# 必须收集用户提供的数据，并以 updated_arguments 回填后才能放行工具调用。
UserInteractionHandler = PermissionApprovalHandler


class PermissionPolicy:
    """通用权限策略层。

    不认识具体工具名,也不解析具体工具参数。工具相关策略由 Tool.check_permission
    提供;这里只追加所有工具都适用的系统边界检查。
    """

    def apply(
        self,
        check: PermissionCheckResult,
        cwd: Path,
        workspace_dir: Path,
    ) -> PermissionCheckResult:
        risk_flags = list(check.risk_flags)

        resolved_cwd = cwd.resolve()
        resolved_workspace = workspace_dir.resolve()
        if not resolved_cwd.is_relative_to(resolved_workspace):
            risk_flags.append("cwd_outside_workspace")

        merged_flags = tuple(dict.fromkeys(risk_flags))
        if merged_flags == check.risk_flags:
            return check

        return replace(
            check,
            risk_flags=merged_flags,
            reason=self._format_reason(check.reason, merged_flags),
        )

    def _format_reason(self, reason: str, risk_flags: tuple[str, ...]) -> str:
        prefix = reason.split("; risks=", 1)[0]
        suffix = f"; risks={', '.join(risk_flags)}" if risk_flags else ""
        return f"{prefix}{suffix}"


class FallbackApprovalHandler:
    """把多个 handler 串成责任链:第一个明确表态(allow/deny)的说了算。

    机制全靠那条共享签名:每个 handler 吃 PermissionRequest、吐 PermissionCheckResult。
    约定一个 handler 返回 `ask` 表示"我弃权,交给下一个";返回 allow/deny 即定案,
    链就此短路。典型用法:[规则式(on_no_match="ask"), 交互式]——规则能自动判的自动判,
    判不了的(规则弃权)才弹终端问人。全员弃权则 fail-closed 拒。

    它本身也满足 handler 签名,所以可以再被嵌进别的链——组合是闭合的。
    """

    def __init__(self, *handlers: PermissionApprovalHandler):
        self.handlers = handlers

    def __call__(self, request: "PermissionRequest") -> PermissionCheckResult:
        for handler in self.handlers:
            result = handler(request)
            if result.decision in ("allow", "deny"):
                return result
            # decision == "ask":该 handler 弃权,继续问下一个
        return PermissionCheckResult(
            "deny",
            "no approval handler made a decision",
            request.check.risk_flags,
            source="fallback_exhausted",
        )


class PermissionRequest:
    """需要外部裁决的权限请求。

    类似 Claude Code 的 canUseTool/permission dialog 边界:工具只能判断
    "需要问",真正是否允许由这里的 handler 决定。当前项目还没有交互 UI,
    所以没有 handler 时 ask 会 fail closed。
    """

    def __init__(
        self,
        tool_call: ToolCall,
        tool: Tool,
        arguments: dict,
        runtime: ToolRuntime,
        check: PermissionCheckResult,
        cwd: Path,
        workspace_dir: Path,
    ):
        self.tool_call = tool_call
        self.tool = tool
        self.arguments = arguments
        self.runtime = runtime
        self.check = check
        self.cwd = cwd
        self.workspace_dir = workspace_dir


class PermissionResolver:
    """把工具权限判断解析成最终执行判定。

    分层目的:
    - Tool.check_permission: 工具自己的 allow/ask/deny 与内容风险识别。
    - PermissionPolicy: 通用系统边界检查,不硬编码具体工具。
    - PermissionApprovalHandler: 用户确认、CLI prompt、测试注入、未来 hooks。
    - ToolExecutor: 只消费最终 allow/deny/ask,不硬编码交互细节。
    """

    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        approval_handler: PermissionApprovalHandler | None = None,
        interaction_handler: UserInteractionHandler | None = None,
    ):
        self.policy = policy or PermissionPolicy()
        self.approval_handler = approval_handler
        self.interaction_handler = interaction_handler

    def resolve(
        self,
        tool_call: ToolCall,
        tool: Tool,
        runtime: ToolRuntime,
        cwd: Path,
        workspace_dir: Path,
    ) -> PermissionCheckResult:
        arguments = dict(tool_call.arguments)
        try:
            tool_check = tool.check_permission(arguments, runtime)
        except Exception as e:
            tool_check = PermissionCheckResult(
                "deny",
                f"{tool.name}: permission check failed: {type(e).__name__}: {e}",
                source="tool_permission_error",
            )

        check = self.policy.apply(tool_check, cwd, workspace_dir)
        if check.decision != "ask":
            return check

        # 对标 Claude Code 的 requiresUserInteraction：即便一般权限规则会放行，
        # 这种调用仍必须经过专属交互 adapter，拿到回填参数后才能执行。
        handler = (
            self.interaction_handler
            if tool.requires_user_interaction
            else self.approval_handler
        )
        handler_kind = "interaction" if tool.requires_user_interaction else "approval"
        if handler is None:
            return check

        try:
            decision = handler(
                PermissionRequest(
                    tool_call=tool_call,
                    tool=tool,
                    arguments=arguments,
                    runtime=runtime,
                    check=check,
                    cwd=cwd,
                    workspace_dir=workspace_dir,
                )
            )
        except Exception as e:
            return PermissionCheckResult(
                "deny",
                f"permission {handler_kind} handler failed: {type(e).__name__}: {e}",
                check.risk_flags,
                source=f"{handler_kind}_handler_error",
            )

        if decision.decision not in ("allow", "deny", "ask"):
            return PermissionCheckResult(
                "deny",
                f"permission {handler_kind} handler returned invalid decision: {decision.decision!r}",
                check.risk_flags,
                source=f"{handler_kind}_handler_error",
            )

        merged_flags = tuple(dict.fromkeys((*check.risk_flags, *decision.risk_flags)))
        if merged_flags != decision.risk_flags:
            return replace(
                decision,
                risk_flags=merged_flags,
                reason=self.policy._format_reason(decision.reason, merged_flags),
            )

        return decision
