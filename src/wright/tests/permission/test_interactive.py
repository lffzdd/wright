from pathlib import Path

from ...executor import ToolExecutor
from ...permission import (
    FallbackApprovalHandler,
    InteractiveApprovalHandler,
    PermissionCheckResult,
    PermissionResolver,
    PermissionSettings,
    RuleBasedApprovalHandler,
)
from ...renderer import SilentRenderer
from ...tools.base import Tool, ToolCall, ToolResult


def _ask_tool(name: str, risk_flags: tuple[str, ...] = ()) -> Tool:
    return Tool(
        name=name,
        description="",
        parameters={},
        call=lambda args, runtime: ToolResult.success({"called": True}),
        check_permission=lambda args, runtime: PermissionCheckResult(
            "ask", f"{name}: needs approval", risk_flags, source="tool"
        ),
    )


class _MockRenderer(SilentRenderer):
    """继承 SilentRenderer，只覆盖 prompt_permission 来模拟用户输入。

    按脚本逐次返回预设答案，记录每次收到的展示信息，完全不碰真 stdin。
    比原来的 input_fn/output_fn 更贴近真实调用路径——走的是同一个 Renderer 接口。
    """

    def __init__(self, *answers: str):
        super().__init__()
        self._answers = list(answers)
        self.prompts: list[dict] = []  # 记录每次 prompt 的参数

    def prompt_permission(
        self,
        tool_name: str,
        subject: str,
        risk_flags: str,
        reason: str,
        offer_always: bool,
    ) -> str:
        self.prompts.append({
            "tool_name": tool_name,
            "subject": subject,
            "risk_flags": risk_flags,
            "reason": reason,
            "offer_always": offer_always,
        })
        return self._answers.pop(0) if self._answers else "n"


def _executor(tool: Tool, handler, tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(
        {tool.name: tool},
        workspace_dir=tmp_path,
        cwd_provider=lambda: tmp_path,
        permission_resolver=PermissionResolver(approval_handler=handler),
    )


def _run(executor: ToolExecutor, call: ToolCall):
    return executor.execute([call])[0].result


# ── 基本 y / n ────────────────────────────────────────────────────────────────

def test_yes_allows(tmp_path):
    renderer = _MockRenderer("y")
    handler = InteractiveApprovalHandler(renderer)
    executor = _executor(_ask_tool("write_file", ("writes_files",)), handler, tmp_path)
    assert _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1")).ok


def test_no_denies(tmp_path):
    renderer = _MockRenderer("n")
    handler = InteractiveApprovalHandler(renderer)
    executor = _executor(_ask_tool("write_file", ("writes_files",)), handler, tmp_path)
    assert not _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1")).ok


def test_empty_input_fail_closed(tmp_path):
    renderer = _MockRenderer("")
    handler = InteractiveApprovalHandler(renderer)
    executor = _executor(_ask_tool("write_file", ("writes_files",)), handler, tmp_path)
    assert not _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1")).ok


# ── 会话记忆:a 之后同工具不再问 ─────────────────────────────────────────────

def test_always_remembers_for_session(tmp_path):
    renderer = _MockRenderer("a")  # 只给一次输入:第二次若再问,pop 会 IndexError
    handler = InteractiveApprovalHandler(renderer)
    executor = _executor(_ask_tool("write_file", ("writes_files",)), handler, tmp_path)

    first = _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1"))
    second = _run(executor, ToolCall("write_file", {"file": "b.txt"}, "c2"))

    assert first.ok and second.ok
    # 第二次直接命中记忆,没有再产生提问
    assert len(renderer.prompts) == 1


def test_always_calls_on_remember_to_persist(tmp_path):
    renderer = _MockRenderer("a")
    remembered: list[str] = []
    handler = InteractiveApprovalHandler(
        renderer, on_remember=remembered.append
    )
    executor = _executor(_ask_tool("write_file", ("writes_files",)), handler, tmp_path)

    assert _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1")).ok
    # 选 a → 落盘钩子收到工具名级规则
    assert remembered == ["write_file"]


# ── 高风险不提供 a;输 a 当作未知输入被拒 ─────────────────────────────────────

def test_always_not_offered_for_heavy_risk(tmp_path):
    renderer = _MockRenderer("a")
    handler = InteractiveApprovalHandler(renderer)
    executor = _executor(
        _ask_tool("execute_command", ("executes_shell",)), handler, tmp_path
    )
    result = _run(executor, ToolCall("execute_command", {"command": "ls"}, "c1"))
    assert not result.ok  # heavy 风险不给 a 选项,'a' 落入拒绝分支
    assert len(renderer.prompts) == 1
    assert not renderer.prompts[0]["offer_always"]


# ── 组合链:规则能判的不打扰人,规则弃权才问 ─────────────────────────────────

def test_fallback_rule_allow_skips_prompt(tmp_path):
    renderer = _MockRenderer()  # 不该被调用:一旦问人就 IndexError
    settings = PermissionSettings.from_dict(
        {"mode": "default", "permissions": {"allow": ["write_file"]}}
    )
    handler = FallbackApprovalHandler(
        RuleBasedApprovalHandler(settings, on_no_match="ask"),
        InteractiveApprovalHandler(renderer),
    )
    executor = _executor(_ask_tool("write_file", ("writes_files",)), handler, tmp_path)
    assert _run(executor, ToolCall("write_file", {"file": "a.txt"}, "c1")).ok
    assert renderer.prompts == []  # 规则直接放行,人没被打扰


def test_fallback_rule_abstains_then_human_decides(tmp_path):
    renderer = _MockRenderer("y")
    settings = PermissionSettings.from_dict({"mode": "default", "permissions": {}})
    handler = FallbackApprovalHandler(
        RuleBasedApprovalHandler(settings, on_no_match="ask"),
        InteractiveApprovalHandler(renderer),
    )
    executor = _executor(_ask_tool("http_request", ("accesses_network",)), handler, tmp_path)
    result = _run(executor, ToolCall("http_request", {"url": "https://x"}, "c1"))
    assert result.ok  # 规则无意见 → 人批准
    assert len(renderer.prompts) == 1


def test_fallback_rule_deny_short_circuits(tmp_path):
    renderer = _MockRenderer()  # 人不该被问到
    settings = PermissionSettings.from_dict(
        {"mode": "default", "permissions": {"deny": ["execute_command"]}}
    )
    handler = FallbackApprovalHandler(
        RuleBasedApprovalHandler(settings, on_no_match="ask"),
        InteractiveApprovalHandler(renderer),
    )
    executor = _executor(
        _ask_tool("execute_command", ("executes_shell",)), handler, tmp_path
    )
    assert not _run(executor, ToolCall("execute_command", {"command": "ls"}, "c1")).ok
    assert renderer.prompts == []

