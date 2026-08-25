"""交互式权限裁决:遇到 ask 就把请求打到终端,让人当场拍 y/n/a。

它和 RuleBasedApprovalHandler 是【同一类东西】——都满足 PermissionApprovalHandler
签名 `(PermissionRequest) -> PermissionCheckResult`,都能直接塞进
`PermissionResolver(approval_handler=...)`。区别只在"拍板的方式":一个查配置自动判,
一个问活人。所以执行器/resolver/子 Agent 那套全不用改,这就是把 handler 做成回调的回报。

相比规则式,交互式多出三个新关注点,代码里都会点到:
1. 状态:`a`(本会话总是允许)要被记住 → handler 实例持有一个 set(它第一次有记忆)。
2. 并发:http_request 是 parallel 工具,可能多线程同时触发 ask;终端的并发保护由
   Renderer 内部的锁负责(ConsoleRenderer._prompt_lock),handler 不再自己持锁。
3. 可测:不能在测试里真等人敲键盘 → 注入一个覆盖了 prompt_permission 的 mock
   Renderer(比原来的 input_fn/output_fn 更贴近真实调用路径)。

fail-closed:空输入、看不懂的输入、读不到终端(EOF)一律当拒——拿不准就不放行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .resolver import PermissionRequest
from .types import PermissionCheckResult

if TYPE_CHECKING:
    from ..renderer import Renderer


class InteractiveApprovalHandler:
    """把 ask 抛给终端前的人来裁决,可选记住"本会话总是允许某工具"。"""

    def __init__(
        self,
        renderer: Renderer,
        on_remember: Callable[[str], None] | None = None,
    ):
        # UI 展示 + 输入收集全部委托给 Renderer。handler 只管决策。
        self._renderer = renderer
        # "别再问"的落盘钩子:用户选 a 时调它把规则固化(如写回 settings.json)。
        # 做成回调而非在 handler 里直接写文件——handler 不该知道"配置存在哪、什么格式",
        # 那是装配层的事;不接这个钩子时 a 就只在本会话内存里生效。
        self._on_remember = on_remember
        # "总是允许"的记忆:按工具名记。本会话恒在内存里(下次同工具直接放行);
        # 若接了 on_remember,则同时落盘,跨会话也生效。
        # 粒度选工具名而非"工具+具体参数":后者几乎不会复用,工具名级才真正省事。
        # 代价是 a execute_command 等于放行该工具任意命令,所以只在低风险时给 a 选项。
        self._always_allow: set[str] = set()

    def __call__(self, request: PermissionRequest) -> PermissionCheckResult:
        tool_name = request.tool.name

        if tool_name in self._always_allow:
            return self._allow(request, f"本会话已记住:总是允许 {tool_name}")

        offer_always = self._allow_always_offered(request)
        answer = self._renderer.prompt_permission(
            tool_name=tool_name,
            subject=_subject_line(request.arguments),
            risk_flags=", ".join(request.check.risk_flags) or "无",
            reason=request.check.reason,
            offer_always=offer_always,
        )

        if answer == "a" and offer_always:
            self._always_allow.add(tool_name)
            if self._on_remember is not None:
                # 落盘成一条 allow 规则;工具名级记忆 → 规则就是裸工具名。
                self._on_remember(tool_name)
            scope = "并已写入配置(跨会话生效)" if self._on_remember else "本会话内"
            return self._allow(request, f"用户批准,记住总是允许 {tool_name}({scope})")
        if answer == "y":
            return self._allow(request, "用户批准本次执行")
        return self._deny(request, f"用户拒绝(输入 {answer!r})")

    # ── 策略 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _allow_always_offered(request: PermissionRequest) -> bool:
        """高风险副作用不提供"总是允许":别让一次回车把整类危险操作永久放行。"""
        heavy = {"executes_shell", "deletes_files", "modifies_git_state"}
        return not (heavy & set(request.check.risk_flags))

    # ── 判定构造 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _allow(request: PermissionRequest, reason: str) -> PermissionCheckResult:
        return PermissionCheckResult(
            "allow", reason, request.check.risk_flags, source="user"
        )

    @staticmethod
    def _deny(request: PermissionRequest, reason: str) -> PermissionCheckResult:
        return PermissionCheckResult(
            "deny", reason, request.check.risk_flags, source="user"
        )


def _subject_line(arguments: dict) -> str:
    for key in ("command", "file", "directory", "url"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return f"{key}={value}"
    return ""

