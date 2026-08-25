"""UI 前端层：所有人机交互的唯一出入口。

职责分两组：
1. **单向输出**（on_* 回调）：Agent 生命周期事件的展示——思考、工具、答案、用量等。
2. **双向交互**（prompt_* 方法）：需要阻塞等待用户输入的场景——权限确认、ask_user 问答。

主循环只跟 Renderer 接口打交道，不关心具体怎么展示/收集输入。
这样同一套 Agent 逻辑可以配不同的 Renderer：

    ConsoleRenderer  → 终端实时输出 + 终端交互（当前默认）
    SilentRenderer   → 什么都不打（跑测试 / 批量任务）
    （未来）JSONRenderer / WebRenderer → 把事件推给前端

交互方法在基类提供 fail-closed 默认实现（拒绝/返回 None），
不支持交互的渲染器（Silent/SubAgent）无需覆盖。
"""

import json
import sys
import threading
from abc import ABC, abstractmethod
from typing import Any

from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from .tools.base import ToolCall, ToolResult


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
    def on_tool_result(self, tool_result: "ToolResult | dict") -> None: ...
    @abstractmethod
    def on_final(self, answer: Any) -> None: ...

    def on_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        context_limit: int | None,
    ) -> None:
        """本轮 token 用量回调(服务端精确值)。默认不输出，子类按需覆盖。"""

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


class ConsoleRenderer(Renderer):
    """终端渲染器（Rich 版）：用 Panel / JSON / Markdown / Rule 对
    "思考 / 回答 / 工具 / 结论"做视觉分层。

    内部用 _phase 记住当前处于哪个流式阶段（reasoning / content / idle），
    只在阶段切换时打印小标题，避免逐 token 重复打标题。

    Rich 自动处理 Windows 终端兼容性和颜色降级（256 → 16 → 无色），
    不再需要手写 ANSI 转义序列。
    """

    def __init__(self) -> None:
        self._phase = "idle"  # "idle" | "reasoning" | "content"
        self._line_started = False
        self._pending_newlines = 0  # 惰性换行计数，_stream_piece 使用
        # highlight=False: 关闭 Rich 对纯文本的自动高亮（数字/URL 等），
        # 避免流式输出时把部分 token 误判为可高亮对象。
        # RichJSON / Markdown 等 Renderable 有自己的高亮逻辑，不受此影响。
        self._console = Console(highlight=False)
        # 全局终端写入锁：序列化所有终端输出，防止并发工具结果与权限确认框交叉。
        #
        # 哪些路径持锁：
        #   prompt_permission / prompt_user / prompt_main_input
        #     —— 用户交互期间持锁，其他输出排队等待。
        #   on_tool_call / on_tool_result / on_command_output
        #   on_checkpoint_error / on_agent_event
        #     —— 可能从 ThreadPoolExecutor worker 或 reader 线程发起，
        #        持锁保证不会插进权限确认框中间。
        # streaming 回调（on_reasoning_delta / on_content_delta）不持锁：
        #   它们始终在主线程的 LLM 流式阶段调用，与 executor 并发窗口不重叠。
        self._prompt_lock = threading.Lock()

    # ----- 流式阶段管理 -----

    def _start_phase(self, phase: str, title: str, title_style: str) -> None:
        """进入一个流式阶段：若是新阶段，先收尾上一个，再打标题。"""
        if self._phase == phase:
            return
        if self._phase in ("reasoning", "content"):
            self._console.print()  # 换行收尾上一个阶段的流式输出
        self._console.print()  # 空行分隔
        self._console.print(title, style=title_style)
        sys.stdout.flush()
        self._phase = phase
        self._line_started = False

    def _end_stream(self) -> None:
        """结束流式阶段（工具调用 / 最终回答前调用）。"""
        if self._phase in ("reasoning", "content"):
            self._console.print()  # 换行收尾
        self._phase = "idle"
        self._line_started = False
        self._pending_newlines = 0  # 清空惰性换行计数

    def _stream_piece(self, piece: str, prefix_style: str, text_style: str) -> None:
        """流式逐块打印，自动在每行开头插入竖线引导符。

        用 pending_newlines 惰性处理换行：收到 \\n 时先计数，
        等到下一段有实际内容时才把积累的换行统一打出来。
        这样纯 \\n chunk（split 后全是空字符串）不会产生没有
        │ 前缀的孤立空行，避免看起来像"模型在返回空白"。
        """
        lines = piece.split("\n")
        for i, line in enumerate(lines):
            if i > 0:
                # 先把换行计入待处理队列，有内容时再一起刷出
                self._pending_newlines += 1
                self._line_started = False
            if line:
                # 有实际内容时才把积累的换行打出来
                for _ in range(self._pending_newlines):
                    self._console.print()
                self._pending_newlines = 0
                if not self._line_started:
                    self._console.print("│ ", end="", style=prefix_style, markup=False)
                    self._line_started = True
                self._console.print(line, end="", style=text_style, markup=False)
        sys.stdout.flush()


    # ----- Renderer 接口实现 -----

    def on_reasoning_delta(self, piece: str) -> None:
        self._start_phase("reasoning", "💭 思考过程", "bold dim bright_black")
        self._stream_piece(piece, prefix_style="dim bright_black", text_style="dim")

    def on_content_delta(self, piece: str) -> None:
        self._start_phase("content", "🤖 模型响应", "bold dim white")
        self._stream_piece(piece, prefix_style="bold dim white", text_style="dim white")

    def on_tool_call(self, tool_call) -> None:
        with self._prompt_lock:
            self._end_stream()
            name = tool_call.name
            arguments = tool_call.arguments

            if arguments:
                body = json.dumps(arguments, ensure_ascii=False, indent=2)
                try:
                    content = RichJSON(body)
                except Exception:
                    content = Text(body, style="dim")
            else:
                content = Text("(无参数)", style="dim italic")

            self._console.print()
            self._console.print(
                Panel(
                    content,
                    title=f"[bold]🔧 {name}[/bold]",
                    title_align="left",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )

            if name == "execute_command":
                self._console.print(Rule("输出", style="dim"))

    def on_command_output(self, line: str) -> None:
        with self._prompt_lock:
            self._console.print(line, end="", style="dim", markup=False)
            sys.stdout.flush()

    def on_checkpoint_error(self, error: str) -> None:
        with self._prompt_lock:
            self._end_stream()
            self._console.print()
            self._console.print(
                Panel(
                    Text(error, style="red"),
                    title="[bold]⚠ checkpoint 保存失败[/bold]",
                    border_style="red",
                    padding=(0, 1),
                )
            )

    def on_agent_event(self, event: dict[str, Any]) -> None:
        with self._prompt_lock:
            self._end_stream()
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

    def on_tool_result(self, tool_result) -> None:
        with self._prompt_lock:
            self._end_stream()
            if hasattr(tool_result, "to_dict"):
                tool_result = tool_result.to_dict()

            if tool_result.get("ok"):
                data = tool_result.get("data")
                body = json.dumps(data, ensure_ascii=False, indent=2)
                try:
                    content = RichJSON(body)
                except Exception:
                    content = Text(body, style="dim")
                self._console.print(
                    Panel(
                        content,
                        title="[bold]✅ 工具结果[/bold]",
                        title_align="left",
                        border_style="green",
                        padding=(0, 1),
                    )
                )
            else:
                err_text = str(tool_result.get("err", "未知错误"))
                self._console.print(
                    Panel(
                        Text(err_text, style="red"),
                        title="[bold]❌ 工具失败[/bold]",
                        title_align="left",
                        border_style="red",
                        padding=(0, 1),
                    )
                )


    def on_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        context_limit: int | None,
    ) -> None:
        self._end_stream()
        inp = prompt_tokens if prompt_tokens is not None else "?"
        out = completion_tokens if completion_tokens is not None else "?"
        tot = total_tokens if total_tokens is not None else "?"

        # 上下文水位 = P+C:模型回复(C)已入队 messages,下次一定是输入的一部分,
        # 所以当前上下文的精确大小 = 本轮输入(P) + 本轮输出(C),两者都是服务端真值。
        if prompt_tokens is not None and completion_tokens is not None and context_limit:
            ctx_size = prompt_tokens + completion_tokens
            water = f"{ctx_size:,} / {context_limit:,} ({ctx_size / context_limit:.1%})"
        else:
            water = f"{inp} / ?"

        self._console.print()
        self._console.print(
            f"[dark_orange bold]tokens[/] "
            f"[dark_orange]输入 {inp} · 输出 {out} · 总计 {tot}[/]"
        )
        self._console.print(
            f"[dark_orange bold]context[/] "
            f"[dark_orange]水位 {water}[/]"
        )

    def on_context_compact(
        self,
        folded_count: int,
        prompt_tokens: int | None,
        context_limit: int | None,
        context_watermark: float,
    ) -> None:
        self._end_stream()

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
            f"[{style}]{msg} · 水位 {ctx_usage}{ctx_pct} · 阈值 {watermark}[/]"
        )

    def on_final(self, answer) -> None:
        self._end_stream()
        self._console.print()

        # 字符串 → Markdown 渲染（LLM 回答通常是 Markdown 格式）
        # 非字符串（dict/list）→ JSON 语法高亮
        if isinstance(answer, str):
            content = Markdown(answer)
        else:
            body = json.dumps(answer, ensure_ascii=False, indent=2)
            try:
                content = RichJSON(body)
            except Exception:
                content = Text(body)

        self._console.print(
            Panel(
                content,
                title="[bold]💬 回答[/bold]",
                title_align="left",
                border_style="green",
                padding=(1, 1),
            )
        )

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
            self._end_stream()

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

    def prompt_user(
        self,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> str | None:
        with self._prompt_lock:
            self._end_stream()

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
                    return None
                if answer:
                    return answer
                self._console.print("回答不能为空，请重新输入。", style="yellow")

    def prompt_main_input(self, prompt_session: Any = None) -> str | None:
        with self._prompt_lock:
            self._end_stream()
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

class SilentRenderer(Renderer):
    """静默渲染器：什么都不输出。用于测试或批量任务。"""

    def on_reasoning_delta(self, piece: str) -> None: ...
    def on_content_delta(self, piece: str) -> None: ...
    def on_tool_call(self, tool_call) -> None: ...
    def on_tool_result(self, tool_result) -> None: ...
    def on_final(self, answer) -> None: ...
