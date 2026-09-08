"""UI 前端层：所有人机交互的唯一出入口。

职责分两组：
1. **单向输出**（on_* 回调）：Agent 生命周期事件的展示——思考、工具、答案、用量等。
2. **双向交互**（prompt_* 方法）：需要阻塞等待用户输入的场景——权限确认、ask_user 问答。

主循环只跟 Renderer 接口打交道，不关心具体怎么展示/收集输入。
这样同一套 Agent 逻辑可以配不同的 Renderer：

    ConsoleRenderer  → 主缓冲 Live 尾巴 + 收口后写入 scrollback
    SilentRenderer   → 什么都不打（跑测试 / 批量任务）
    （未来）JSONRenderer / WebRenderer → 把事件推给前端

交互方法在基类提供 fail-closed 默认实现（拒绝/返回 None），
不支持交互的渲染器（Silent/SubAgent）无需覆盖。
"""

import json
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML
from rich.console import Console, Group
from rich.json import JSON as RichJSON
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from .tools.base import ToolCall, ToolResult

_COMMAND_OUTPUT_LINES = 24


class Renderer(ABC):
    """UI 前端接口。主循环按 ReAct 的生命周期回调 on_* 方法展示事件，
    按需调用 prompt_* 方法进行双向交互。"""

    @abstractmethod
    def on_reasoning_delta(self, piece: str) -> None: ...
    @abstractmethod
    def on_content_delta(self, piece: str) -> None: ...
    @abstractmethod
    def on_tool_call(self, tool_call: ToolCall | dict) -> None: ...
    @abstractmethod
    def on_tool_result(
        self, tool_call: ToolCall | dict, tool_result: "ToolResult | dict",
    ) -> None: ...
    @abstractmethod
    def on_final(self, answer: Any) -> None: ...

    def on_turn_begin(self) -> None:
        """A new LLM turn is starting. Default: no-op."""

    def on_completion_rejected(self, issues: Any = ()) -> None:
        """Structural completion checks rejected this candidate. Default: no-op."""

    def on_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        context_limit: int | None,
    ) -> None:
        """本轮 token 用量回调(服务端精确值)。默认不输出，子类按需覆盖。"""

    def on_usage_summary(
        self, prompt_tokens: int, completion_tokens: int, total_tokens: int,
    ) -> None:
        """当前任务累计消费。"""

    def on_context_compact(
        self,
        folded_count: int,
        prompt_tokens: int | None,
        context_limit: int | None,
        context_watermark: float,
    ) -> None:
        """上下文压缩回调。默认不输出，子类按需覆盖。"""

    def on_command_output(self, line: str) -> None:
        """命令流式输出回调。默认不输出，子类按需覆盖。"""

    def on_checkpoint_error(self, error: str) -> None:
        """Checkpoint 持久化失败。默认不输出，交互渲染器应明确告警。"""

    def on_agent_event(self, event: dict[str, Any]) -> None:
        """子 Agent 控制面事件。默认不输出。"""

    # ── 双向交互（子类按能力覆盖，默认 fail-closed） ──

    def prompt_permission(
        self,
        tool_name: str,
        subject: str,
        risk_flags: str,
        reason: str,
        offer_always: bool,
    ) -> str:
        """展示权限确认请求并收集用户选择，返回原始输入字符串。

        默认实现直接返回 ``"n"``（fail-closed），不支持交互的渲染器
        （SilentRenderer / SubAgentRenderer）继承此默认即可。
        """
        return "n"

    def prompt_user(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> str | None:
        """展示 ask_user 问题并阻塞等待用户回答。

        返回用户输入的非空字符串；返回 ``None`` 表示用户取消（Ctrl-C/EOF）
        或渲染器不支持交互。默认实现返回 ``None``。
        """
        return None

    def prompt_main_input(self, prompt_session: Any = None) -> str | None:
        """主 REPL 循环展示输入提示并等待用户指令。返回 None 表示退出。"""
        return None


@dataclass
class _LiveTool:
    call: Any
    result: dict | None = None
    output: str = ""


class ConsoleRenderer(Renderer):
    """主缓冲交互渲染器：当前这一轮在可见尾巴里原地更新，收口后固化进 scrollback。

    不进入备用缓冲区。TTY 上用 Rich Live 刷新预览；非 TTY 只在收口时打印定稿。
    """

    def __init__(self) -> None:
        # highlight=False: 关闭 Rich 对纯文本的自动高亮（数字/URL 等），
        # 避免流式输出时把部分 token 误判为可高亮对象。
        # RichJSON / Markdown 等 Renderable 有自己的高亮逻辑，不受此影响。
        self._console = Console(
            highlight=False,
            force_terminal=True if sys.stdout.isatty() else None,
        )
        # 全局终端写入锁：序列化所有终端输出，防止并发工具结果与权限确认框交叉。
        #
        # 哪些路径持锁：
        #   prompt_permission / prompt_user / prompt_main_input
        #     —— 用户交互期间持锁，其他输出排队等待。
        #   on_tool_call / on_tool_result / on_command_output
        #   on_checkpoint_error / on_agent_event
        #     —— 可能从 ThreadPoolExecutor worker 或 reader 线程发起，
        #        持锁保证不会插进权限确认框中间。
        # streaming 回调也持锁：Live 刷新与 worker 输出会重叠。
        self._prompt_lock = threading.Lock()
        self._live: Live | None = None
        self._reasoning = ""
        self._content = ""
        self._final_answer: Any = None
        self._usage_line: str | None = None
        self._tools: list[_LiveTool] = []
        self._show_settled = False

    def _can_live(self) -> bool:
        # StringIO / piped captures have no isatty; don't start Live there.
        stream = getattr(self._console, "file", None)
        checker = getattr(stream, "isatty", None)
        if checker is None:
            return False
        try:
            if not checker():
                return False
        except Exception:
            return False
        return bool(self._console.is_terminal)

    def _has_stream(self) -> bool:
        return bool(self._reasoning or self._content or self._final_answer is not None)

    def _has_preview(self) -> bool:
        return self._has_stream() or bool(self._tools)

    def _json_body(self, value: Any) -> Any:
        body = json.dumps(value, ensure_ascii=False, indent=2)
        try:
            return RichJSON(body)
        except Exception:
            return Text(body, style="dim")

    def _answer_panel(self, answer: Any) -> Panel:
        if isinstance(answer, str):
            content: Any = Markdown(answer)
        else:
            content = self._json_body(answer)
        return Panel(
            content,
            title="[bold]💬 回答[/bold]",
            title_align="left",
            border_style="green",
            padding=(1, 1),
        )

    def _draft_text(self, text: str) -> Text:
        prefixed = "\n".join(
            f"│ {line}" if line else "│" for line in text.split("\n")
        )
        return Text(prefixed, style="dim white")

    def _tool_panel(self, block: _LiveTool) -> Panel:
        name = _tool_call_name(block.call)
        if block.result is None:
            arguments = getattr(block.call, "arguments", None)
            if arguments is None and isinstance(block.call, dict):
                arguments = block.call.get("arguments")
            body: Any = (
                Text("(无参数)", style="dim italic")
                if not arguments
                else self._json_body(arguments)
            )
            if block.output:
                lines = block.output.splitlines()
                clipped = lines[-_COMMAND_OUTPUT_LINES:]
                prefix = "" if len(lines) <= _COMMAND_OUTPUT_LINES else "…\n"
                body = Group(body, Text(prefix + "\n".join(clipped), style="dim"))
            return Panel(
                body,
                title=f"[bold]🔧 {name}[/bold]",
                title_align="left",
                border_style="yellow",
                padding=(0, 1),
            )
        if block.result.get("ok"):
            return Panel(
                self._json_body(block.result.get("data")),
                title=f"[bold]✅ {name}[/bold]",
                title_align="left",
                border_style="green",
                padding=(0, 1),
            )
        return Panel(
            Text(str(block.result.get("err", "未知错误")), style="red"),
            title=f"[bold]❌ {name}[/bold]",
            title_align="left",
            border_style="red",
            padding=(0, 1),
        )

    def _current_renderable(self, *, settled: bool) -> Any:
        parts: list[Any] = []
        if self._reasoning:
            parts.append(Text("💭 思考过程", style="bold dim bright_black"))
            parts.append(Text(self._reasoning, style="dim"))
        if self._final_answer is not None and settled:
            parts.append(self._answer_panel(self._final_answer))
        elif self._content:
            parts.append(Text("💬 回答", style="bold dim white"))
            parts.append(self._draft_text(self._content))
        for block in self._tools:
            parts.append(self._tool_panel(block))
        if self._usage_line:
            parts.append(Text.from_markup(self._usage_line))
        if not parts:
            return Text("")
        if len(parts) == 1:
            return parts[0]
        return Group(*parts)

    def _suspend_live(self) -> None:
        live = self._live
        if live is None:
            return
        self._live = None
        try:
            live.stop()
        except Exception:
            pass

    def _ensure_live(self) -> None:
        if self._live is not None or not self._can_live() or not self._has_preview():
            return
        self._live = Live(
            get_renderable=self._preview,
            console=self._console,
            auto_refresh=True,
            refresh_per_second=16,
            transient=True,  # 权限确认要能擦掉预览；收口前改成 False 把最后一帧冻进 scrollback
            redirect_stdout=False,
            redirect_stderr=False,
            vertical_overflow="ellipsis",
            screen=False,
        )
        self._live.start()

    def _refresh_live(self) -> None:
        live = self._live
        if live is None:
            self._ensure_live()
            return
        try:
            live.update(self._current_renderable(settled=False), refresh=False)
        except Exception:
            pass

    def _reset_stream(self) -> None:
        self._reasoning = ""
        self._content = ""
        self._final_answer = None
        self._usage_line = None

    def _reset_all(self) -> None:
        self._reset_stream()
        self._tools = []

    def _print_settled(self, renderable: Any) -> None:
        if isinstance(renderable, Text) and not renderable.plain:
            return
        self._console.print()
        self._console.print(renderable)

    def _preview(self) -> Any:
        return self._current_renderable(settled=self._show_settled)

    def _freeze(self, renderable: Any) -> None:
        """把当前尾巴定格进主缓冲历史：有 Live 就留最后一帧，否则直接打印。"""
        live = self._live
        if live is not None:
            try:
                self._show_settled = True
                live.update(renderable, refresh=True)
                live.transient = False
                self._suspend_live()
                return
            except Exception:
                self._suspend_live()
            finally:
                self._show_settled = False
        self._print_settled(renderable)

    def _commit(self, *, settled: bool) -> None:
        if not self._has_preview():
            self._suspend_live()
            return
        self._freeze(self._current_renderable(settled=settled))
        self._reset_all()

    def _commit_stream_if_any(self) -> None:
        if not self._has_stream() or self._tools:
            return
        self._freeze(self._current_renderable(settled=False))
        self._reset_stream()

    def _commit_tools_if_done(self) -> None:
        if not self._tools or any(block.result is None for block in self._tools):
            return
        self._commit(settled=True)

    def _find_tool(self, tool_call) -> _LiveTool | None:
        call_id = getattr(tool_call, "id", None)
        if not call_id and isinstance(tool_call, dict):
            call_id = tool_call.get("id")
        if call_id:
            for block in self._tools:
                block_id = getattr(block.call, "id", None)
                if not block_id and isinstance(block.call, dict):
                    block_id = block.call.get("id")
                if block_id == call_id:
                    return block
        name = _tool_call_name(tool_call)
        running = [
            block for block in self._tools
            if block.result is None and _tool_call_name(block.call) == name
        ]
        return running[-1] if running else None

    def _running_command(self) -> _LiveTool | None:
        running = [
            block for block in self._tools
            if block.result is None and _tool_call_name(block.call) == "execute_command"
        ]
        return running[-1] if running else None

    # ----- Renderer 接口实现 -----

    def on_turn_begin(self) -> None:
        with self._prompt_lock:
            self._commit(settled=False)

    def on_reasoning_delta(self, piece: str) -> None:
        if not piece:
            return
        with self._prompt_lock:
            self._reasoning += piece
            self._refresh_live()

    def on_content_delta(self, piece: str) -> None:
        if not piece:
            return
        with self._prompt_lock:
            self._content += piece
            self._refresh_live()

    def on_tool_call(self, tool_call) -> None:
        with self._prompt_lock:
            self._commit_stream_if_any()
            self._tools.append(_LiveTool(call=tool_call))
            self._refresh_live()

    def on_command_output(self, line: str) -> None:
        with self._prompt_lock:
            block = self._running_command()
            if block is None:
                return
            block.output += line
            self._refresh_live()

    def on_checkpoint_error(self, error: str) -> None:
        with self._prompt_lock:
            self._suspend_live()
            self._console.print()
            self._console.print(
                Panel(
                    Text(error, style="red"),
                    title="[bold]⚠ checkpoint 保存失败[/bold]",
                    border_style="red",
                    padding=(0, 1),
                )
            )
            self._ensure_live()

    def on_agent_event(self, event: dict[str, Any]) -> None:
        with self._prompt_lock:
            self._suspend_live()
            status = str(event.get("status", "unknown"))
            task_id = str(event.get("task_id", "?"))
            depth = event.get("depth", "?")
            task = str(event.get("task", ""))
            if len(task) > 100:
                task = task[:97] + "..."
            style = {
                "running": "cyan",
                "completed": "green",
                "failed": "red",
                "cancelled": "yellow",
                "timed_out": "yellow",
            }.get(status, "dim")
            line = Text(f"agent {task_id} · d{depth} · {status}", style=style)
            if task:
                line.append(f" {task}", style="dim")
            self._console.print(line)
            self._ensure_live()

    def on_tool_result(self, tool_call, tool_result) -> None:
        with self._prompt_lock:
            if hasattr(tool_result, "to_dict"):
                tool_result = tool_result.to_dict()
            block = self._find_tool(tool_call)
            if block is None:
                block = _LiveTool(call=tool_call)
                self._tools.append(block)
            block.result = tool_result
            self._refresh_live()
            self._commit_tools_if_done()

    def on_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        context_limit: int | None,
    ) -> None:
        inp = prompt_tokens if prompt_tokens is not None else "?"
        out = completion_tokens if completion_tokens is not None else "?"
        tot = total_tokens if total_tokens is not None else "?"
        line = (
            f"[dark_orange bold]tokens 本次请求[/] "
            f"[dark_orange]输入 {inp} · 输出 {out} · 合计 {tot}[/]"
        )
        with self._prompt_lock:
            attached = self._has_preview()
            self._usage_line = line
            if attached:
                self._refresh_live()
            else:
                self._console.print()
                self._console.print(line)
                self._usage_line = None

    def on_usage_summary(
        self, prompt_tokens: int, completion_tokens: int, total_tokens: int,
    ) -> None:
        with self._prompt_lock:
            self._commit(settled=True)
            self._console.print(
                f"[dark_orange bold]tokens 当前任务累计（已报告）[/] "
                f"[dark_orange]输入 {prompt_tokens:,} · 输出 {completion_tokens:,} "
                f"· 合计 {total_tokens:,}[/]"
            )

    def on_context_compact(
        self,
        folded_count: int,
        prompt_tokens: int | None,
        context_limit: int | None,
        context_watermark: float,
    ) -> None:
        with self._prompt_lock:
            self._suspend_live()
            if prompt_tokens is not None and context_limit:
                ctx_usage = f"{prompt_tokens:,} / {context_limit:,}"
                ctx_pct = f" ({prompt_tokens / context_limit:.1%})"
            else:
                ctx_usage = "? / ?"
                ctx_pct = ""
            watermark = f"{context_watermark:.0%}"
            if folded_count > 0:
                msg = f"已折叠 {folded_count} 条旧工具结果"
                style = "dark_orange"
            else:
                msg = "上下文已超水位,但暂无可折叠旧工具结果"
                style = "yellow"
            self._console.print()
            self._console.print(
                f"[{style} bold]context compact[/] "
                f"[{style}]{msg} · 预计占用 {ctx_usage}{ctx_pct} · 阈值 {watermark}[/]"
            )
            self._ensure_live()

    def on_completion_rejected(self, issues: Any = ()) -> None:
        parts = []
        for issue in issues or ():
            message = getattr(issue, "message", None)
            if message is None and isinstance(issue, dict):
                message = issue.get("message")
            if message:
                parts.append(str(message))
        detail = "；".join(parts) if parts else "未说明原因"
        with self._prompt_lock:
            self._commit(settled=False)
            self._console.print()
            self._console.print(f"[yellow]完成检查未通过，继续工作[/] [dim]{detail}[/]")

    def on_final(self, answer) -> None:
        with self._prompt_lock:
            self._final_answer = answer
            self._commit(settled=True)

    # ── 双向交互 ──

    def prompt_permission(
        self,
        tool_name: str,
        subject: str,
        risk_flags: str,
        reason: str,
        offer_always: bool,
    ) -> str:
        with self._prompt_lock:
            self._suspend_live()

            info = Text()
            info.append("工具: ", style="bold")
            info.append(f"{tool_name}\n")
            if subject:
                info.append("参数: ", style="bold")
                info.append(f"{subject}\n")
            info.append("风险: ", style="bold")
            info.append(f"{risk_flags}\n")
            info.append("说明: ", style="bold")
            info.append(reason)

            self._console.print()
            self._console.print(
                Panel(
                    info,
                    title="[bold]⚠️  需要权限确认[/bold]",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )

            choices = "  [bold]y[/]=允许一次  [bold]n[/]=拒绝"
            if offer_always:
                choices += "  [bold]a[/]=本会话总是允许该工具"
            self._console.print(choices)

            prompt_text = HTML("  <b><ansiyellow>允许执行? </ansiyellow></b>")
            try:
                return prompt(prompt_text).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "n"
            finally:
                self._ensure_live()

    def prompt_user(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> str | None:
        with self._prompt_lock:
            self._suspend_live()

            body = Text()
            body.append(question, style="cyan")
            if context:
                body.append(f"\n{context}", style="dim")
            if options:
                body.append("\n")
                for idx, opt in enumerate(options, start=1):
                    body.append(f"\n  {idx}. {opt}", style="dim")

            self._console.print()
            self._console.print(
                Panel(
                    body,
                    title="[bold]❓ 需要你的回答[/bold]",
                    border_style="cyan",
                    padding=(1, 2),
                )
            )

            prompt_text = HTML("<b><ansicyan>你的回答 ❯ </ansicyan></b>")
            while True:
                try:
                    answer = prompt(prompt_text).strip()
                except (EOFError, KeyboardInterrupt):
                    self._console.print()
                    self._ensure_live()
                    return None
                if answer:
                    self._ensure_live()
                    return answer
                self._console.print("回答不能为空，请重新输入。", style="yellow")

    def prompt_main_input(self, prompt_session: Any = None) -> str | None:
        with self._prompt_lock:
            self._commit(settled=True)
            self._console.print()

            prompt_text = HTML(
                "<b><ansicyan>╭─ 💬 你的指令 </ansicyan><ansibrightblack>(输入 /exit 退出)</ansibrightblack></b>\n"
                "<b><ansicyan>╰─❯ </ansicyan></b>"
            )

            try:
                if prompt_session is not None:
                    val = prompt_session.prompt(prompt_text).strip()
                else:
                    val = prompt(prompt_text).strip()
            except (EOFError, KeyboardInterrupt):
                self._console.print()
                return None

            if val and val not in ("/exit", "/quit"):
                # 按下回车后：抹掉两行输入提示符，原地替换为与最终答案规格一致的舒适卡片
                sys.stdout.write("\033[A\033[2K\033[A\033[2K\r")
                sys.stdout.flush()
                self._console.print(
                    Panel(
                        Markdown(val),
                        title="[bold]🧑 你的提问[/bold]",
                        title_align="left",
                        border_style="cyan",
                        padding=(1, 2),
                    )
                )
            return val

    def render_session_history(
        self,
        session_state: Any,
        max_turns: int = 5,
        pager: bool = False,
    ) -> None:
        """Resume 时展示历史对话摘要；/history all 时以 pager 全量展示。

        pager=False（默认）：最近 max_turns 轮，回答截 300 字符，直接打终端。
        pager=True：全量不截断，用 Rich pager（less 风格）包住，
                   用户可 j/k 滚动、/ 搜索、q 退出。
        """
        # 只取 route="final" 的轮次（跳过纯工具调用轮和 invalid 轮）
        final_turns = [t for t in session_state.turns if t.route == "final"]
        if not final_turns:
            return

        recent = final_turns if pager else final_turns[-max_turns:]

        # 建立 message_id → index 的反查表，用于找 user 消息
        id_to_idx: dict[str, int] = {
            r.id: i for i, r in enumerate(session_state.message_records)
        }

        pairs: list[tuple[str, str]] = []  # (user_text, final_answer)
        for turn in recent:
            # 提取 final_answer
            final_answer = turn.parsed.get("final_answer", "")
            if not isinstance(final_answer, str):
                try:
                    final_answer = json.dumps(final_answer, ensure_ascii=False)
                except Exception:
                    final_answer = str(final_answer)
            if not final_answer.strip():
                continue

            # 在 message_records 里找这轮 assistant 消息的前一条「真实用户输入」：
            # 排除记忆系统注入（<system-reminder> 开头）和工具结果回注（JSON tool_results）
            asst_idx = id_to_idx.get(turn.message_id, -1)
            user_text = ""
            for i in range(asst_idx - 1, -1, -1):
                rec = session_state.message_records[i]
                role = rec.message.get("role", "")
                if role == "user":
                    content = rec.message.get("content", "")
                    if not isinstance(content, str):
                        continue
                    stripped = content.lstrip()
                    # 跳过各类系统注入消息，只保留真实用户输入：
                    #   - 记忆注入：<system-reminder> 开头
                    #   - 工具结果回注：{"tool_results": ...}
                    #   - 验证器反馈：{"verification_feedback": ...}
                    #   - 子任务通知：<task-notification> 开头
                    if stripped.startswith("<system-reminder>"):
                        continue
                    if stripped.startswith("<task-notification>"):
                        continue
                    if stripped.startswith("{") and any(
                        k in stripped[:120]
                        for k in ("tool_results", "verification_feedback")
                    ):
                        continue

                    user_text = content
                    break

            if user_text or final_answer:
                pairs.append((user_text.strip(), final_answer.strip()))

        # 去重：同一 user 问题因验证重试产生多个 final turn 时，
        # 只保留最后一次（最终通过验证的那条回答）
        deduped: list[tuple[str, str]] = []
        for user_text, answer_text in pairs:
            if deduped and deduped[-1][0] == user_text:
                deduped[-1] = (user_text, answer_text)  # 用最新的回答覆盖
            else:
                deduped.append((user_text, answer_text))
        pairs = deduped

        if not pairs:
            return

        total_final = len(final_turns)
        shown = len(pairs)
        if pager:
            suffix = f"共 {shown} 轮（完整）"
            title_label = "📜 历史完整记录"
        else:
            suffix = f"最近 {shown} 轮" if total_final > shown else f"共 {shown} 轮"
            title_label = "📜 历史对话摘要"
        sid = getattr(session_state, "session_id", "?")

        def _render_to(con: Console) -> None:
            con.print()
            con.print(
                Rule(
                    f"[dim]{title_label}  session {sid} · {suffix}[/dim]",
                    style="dim",
                )
            )

            for user_text, answer_text in pairs:
                con.print()
                if user_text:
                    if pager:
                        # 全量模式：完整展示用户输入（可能多行）
                        con.print(Text(f"  🧑 {user_text}", style="dim cyan"))
                    else:
                        # 摘要模式：单行截断，保持简洁
                        display_user = user_text.replace("\n", " ")
                        if len(display_user) > 120:
                            display_user = display_user[:117] + "..."
                        con.print(Text(f"  🧑 {display_user}", style="dim cyan"))

                # 回答：pager 模式不截断；摘要模式截 300 字符
                display_answer = answer_text
                truncated = False
                if not pager and len(display_answer) > 300:
                    display_answer = display_answer[:297] + "..."
                    truncated = True
                try:
                    answer_renderable = Markdown(display_answer)
                except Exception:
                    answer_renderable = Text(display_answer, style="dim")

                con.print(
                    Panel(
                        answer_renderable,
                        title="[dim]🤖 回答[/dim]",
                        title_align="left",
                        border_style="dim",
                        padding=(0, 1),
                        subtitle="[dim italic]（已截断）[/dim italic]" if truncated else None,
                    )
                )

            con.print()
            if not pager:
                con.print(
                    Rule("[dim]↑ 历史  ·  以下为本次对话[/dim]", style="dim")
                )
                con.print()

        if pager:
            with self._console.pager(styles=True):
                _render_to(self._console)
        else:
            _render_to(self._console)


def _tool_call_name(tool_call) -> str:
    name = getattr(tool_call, "name", None)
    if not name and isinstance(tool_call, dict):
        name = tool_call.get("name")
    return str(name or "tool")


class SilentRenderer(Renderer):
    """静默渲染器：什么都不输出。用于测试或批量任务。"""

    def on_reasoning_delta(self, piece: str) -> None: ...
    def on_content_delta(self, piece: str) -> None: ...
    def on_tool_call(self, tool_call) -> None: ...
    def on_tool_result(self, tool_call, tool_result) -> None: ...
    def on_final(self, answer) -> None: ...
