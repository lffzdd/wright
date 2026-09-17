"""交互式权限裁决:遇到 ask 就把请求打到终端,让人当场拍 y/n/a。

它和 RuleBasedApprovalHandler 是【同一类东西】——都满足 PermissionApprovalHandler
签名 `(PermissionRequest) -> PermissionCheckResult`,都能直接塞进
`PermissionResolver(approval_handler=...)`。区别只在"拍板的方式":一个查配置自动判,
一个问活人。所以执行器/resolver/子 Agent 那套全不用改,这就是把 handler 做成回调的回报。

相比规则式,交互式多出三个新关注点,代码里都会点到:
1. 状态:`a`(本会话总是允许)要被记住 → handler 实例持有一个 set(它第一次有记忆)。
2. 并发:http_request 是 parallel 工具,可能多线程同时触发 ask;stdin 只由 REPL
   收集线程读取,请求经 InteractionHub 排队,handler 不再碰终端锁。
3. 可测:不能在测试里真等人敲键盘 → 注入一个覆盖了 prompt_permission 的 mock
   Renderer(比原来的 input_fn/output_fn 更贴近真实调用路径)。

fail-closed:空输入、看不懂的输入、读不到终端(EOF)一律当拒——拿不准就不放行。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from glob import escape
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..paths import user_permission_settings_path
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
        # Remember scoped rules, never a bare capability name.  A single
        # approval must not silently authorize every future URL or file.
        self._always_allow: set[str] = set()

    def __call__(self, request: PermissionRequest) -> PermissionCheckResult:
        tool_name = request.tool.name
        remembered_rule = _remember_rule(request)

        if remembered_rule is not None and remembered_rule in self._always_allow:
            return self._allow(request, f"本会话已记住:允许 {remembered_rule}")

        offer_always = remembered_rule is not None and self._allow_always_offered(request)
        try:
            self._renderer.on_tool_phase(request.tool_call, "awaiting_approval")
        except Exception:
            # Rendering a state hint must not change the permission outcome.
            pass
        answer = self._renderer.prompt_permission(
            tool_name=tool_name,
            subject=_subject_line(request.arguments),
            risk_flags=", ".join(request.check.risk_flags) or "无",
            reason=request.check.reason,
            offer_always=offer_always,
            remember_rule=remembered_rule or "",
            remember_persists=bool(offer_always and self._on_remember is not None),
            revoke_hint=(
                f"Remove this rule from {_permission_settings_path()}."
                if offer_always and self._on_remember is not None
                else ""
            ),
        )

        if answer == "a" and offer_always:
            assert remembered_rule is not None
            self._always_allow.add(remembered_rule)
            if self._on_remember is not None:
                self._on_remember(remembered_rule)
            scope = "并已写入配置(跨会话生效)" if self._on_remember else "本会话内"
            return self._allow(request, f"用户批准,记住允许 {remembered_rule}({scope})")
        if answer == "y":
            return self._allow(request, "用户批准本次执行")
        return self._deny(request, f"用户拒绝(输入 {answer!r})")

    # ── 策略 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _allow_always_offered(request: PermissionRequest) -> bool:
        """高风险副作用不提供"总是允许":别让一次回车把整类危险操作永久放行。"""
        heavy = {
            "executes_shell", "deletes_files", "modifies_git_state",
            "mutates_remote_state",
        }
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


def _remember_rule(request: PermissionRequest) -> str | None:
    """Return a useful, bounded scope for the UI's persistent approval."""
    tool_name = request.tool.name
    arguments = request.arguments
    file_value = arguments.get("file")
    if isinstance(file_value, str) and file_value:
        parent = PurePosixPath(file_value).parent.as_posix()
        subject = escape(file_value) if parent == "." else f"{escape(parent)}/*"
        return f"{tool_name}({subject})"
    url_value = arguments.get("url")
    if isinstance(url_value, str) and url_value:
        parsed = urlsplit(url_value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"{tool_name}({escape(parsed.scheme + '://' + parsed.netloc)}*)"
    return None


def _permission_settings_path() -> Path:
    configured = os.getenv("WRIGHT_PERMISSION_CONFIG", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else user_permission_settings_path()
    )
