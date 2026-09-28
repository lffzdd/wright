"""Rich console renderer for the interactive CLI.

The shared Renderer contract does not import this module.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Any

from rich.console import Console, Group
from rich.json import JSON as RichJSON
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from ..i18n import format_issues, present, t
from ..rendering.contracts import Renderer
from ..rendering.history import collect_history_entries


def _tool_error(result: dict) -> str:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    code = str(data.get("display_code") or "")
    params = data.get("display_params") if isinstance(data.get("display_params"), dict) else {}
    return present(code, params, fallback=str(result.get("err") or t("error.unknown")))


def _choice_text(choice: dict) -> tuple[str, str, str]:
    choice_id = str(choice.get("id") or "")
    label = present(f"permission.choice.{choice_id}", fallback=str(choice.get("label") or ""))
    scope_keys = {
        "allow_once": "permission.scope.once",
        "deny": "permission.scope.none",
    }
    persistence_keys = {
        "allow_once": "permission.persistence.none",
        "deny": "permission.persistence.none",
        "allow_session_directory": "permission.persistence.session_directory",
        "allow_persistent_directory": "permission.persistence.persistent_directory",
        "allow_session_rule": "permission.persistence.session_rule",
        "allow_persistent_rule": "permission.persistence.persistent_rule",
    }
    scope = present(scope_keys.get(choice_id, ""), fallback=str(choice.get("scope") or ""))
    persistence = present(
        persistence_keys.get(choice_id, ""),
        fallback=str(choice.get("persistence") or ""),
    )
    return label, scope, persistence

_COMMAND_OUTPUT_LINES = 24


@dataclass
class _LiveTool:
    call: Any
    result: dict | None = None
    output: str = ""
    phase: str = "planned"


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
        # 全局终端写入锁：序列化绘制，不覆盖 stdin 等待。
        # 读键盘只在 REPL 收集线程；Agent 线程通过 InteractionHub 等待回复。
        self._prompt_lock = threading.Lock()
        self._live: Live | None = None
        self._reasoning = ""
        self._content = ""
        self._final_answer: Any = None
        self._usage_line: str | None = None
        self._tools: list[_LiveTool] = []

    def _can_live(self) -> bool:
        # StringIO / piped captures have no isatty; don't start Live there.
        stream = getattr(self._console, "file", None)
        checker = getattr(stream, "isatty", None)
        if checker is None:
            return False
        try:
            if not checker():
                return False
        except (OSError, ValueError):
            return False
        return bool(self._console.is_terminal)

    def _has_stream(self) -> bool:
        return bool(self._reasoning or self._content or self._final_answer is not None)

    def _has_preview(self) -> bool:
        return self._has_stream() or bool(self._tools)

    def _json_body(self, value: Any) -> Any:
        try:
            return RichJSON.from_data(value)
        except (TypeError, ValueError):
            return Text(str(value), style="dim")

    def _answer_panel(self, answer: Any) -> Panel:
        if isinstance(answer, str):
            content: Any = Markdown(answer)
        else:
            content = self._json_body(answer)
        return Panel(
            content,
            title=f"[bold]{t('tool.answer')}[/bold]",
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
                Text(t("tool.no_arguments"), style="dim italic")
                if not arguments
                else self._json_body(arguments)
            )
            if block.output:
                lines = block.output.splitlines()
                clipped = lines[-_COMMAND_OUTPUT_LINES:]
                prefix = "" if len(lines) <= _COMMAND_OUTPUT_LINES else "…\n"
                body = Group(body, Text(prefix + "\n".join(clipped), style="dim"))
            phase = block.phase.replace("_", " ")
            return Panel(
                body,
                title=f"[bold]🔧 {name} · {phase}[/bold]",
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
            Text(_tool_error(block.result), style="red"),
            title=f"[bold]❌ {name}[/bold]",
            title_align="left",
            border_style="red",
            padding=(0, 1),
        )

    def _current_renderable(self, *, settled: bool) -> Any:
        parts: list[Any] = []
        if self._reasoning:
            parts.append(Text(t("tool.thinking"), style="bold dim bright_black"))
            parts.append(Text(self._reasoning, style="dim"))
        if self._final_answer is not None and settled:
            parts.append(self._answer_panel(self._final_answer))
        elif self._content:
            parts.append(Text(t("tool.answer"), style="bold dim white"))
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
        except Exception:  # Live 拆掉时终端可能已不可写
            pass

    def _ensure_live(self) -> None:
        if self._live is not None or not self._can_live() or not self._has_preview():
            return
        # 把当前画面的快照交给 Live，不要传 get_renderable。
        # Live 的 auto-refresh 在后台线程跑；回调里读 _reasoning/_tools 会和
        # 持有 _prompt_lock 的主线程打架。回调里再加锁又会和 live.update()
        # 形成死锁（主线程: prompt_lock→live._lock；刷新线程相反）。
        self._live = Live(
            self._current_renderable(settled=False),
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
        except Exception:  # 刷新失败就丢这一帧
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

    def _freeze(self, renderable: Any) -> None:
        """把当前尾巴定格进主缓冲历史：有 Live 就留最后一帧，否则直接打印。"""
        live = self._live
        if live is not None:
            try:
                live.update(renderable, refresh=True)
                live.transient = False
                self._suspend_live()
                return
            except Exception:  # 定格失败则改走普通 print
                self._suspend_live()
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

    def on_tool_phase(self, tool_call, phase: str) -> None:
        with self._prompt_lock:
            block = self._find_tool(tool_call)
            if block is not None:
                block.phase = phase
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
                    title=f"[bold]{t('checkpoint.failed_title')}[/bold]",
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
        line = "[dark_orange]" + t("usage.request", input=inp, output=out, total=tot) + "[/]"
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
                "[dark_orange]"
                + t(
                    "usage.task",
                    input=f"{prompt_tokens:,}",
                    output=f"{completion_tokens:,}",
                    total=f"{total_tokens:,}",
                )
                + "[/]"
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
                msg = t("context.compacted", count=folded_count)
                style = "dark_orange"
            else:
                msg = t("context.no_fold")
                style = "yellow"
            self._console.print()
            self._console.print(
                f"[{style}]"
                + t("context.usage", message=msg, usage=ctx_usage, percent=ctx_pct, watermark=watermark)
                + "[/]"
            )
            self._ensure_live()

    def on_completion_rejected(self, issues: Any = ()) -> None:
        detail = format_issues(issues)
        with self._prompt_lock:
            self._commit(settled=False)
            self._console.print()
            self._console.print(f"[yellow]{t('verification.rejected', detail=detail)}[/]")

    def on_final(self, answer) -> None:
        with self._prompt_lock:
            self._final_answer = answer
            self._commit(settled=True)

    def on_system_notice(self, text: str, *, code: str = "", params: dict | None = None) -> None:
        del code, params
        with self._prompt_lock:
            self._suspend_live()
            self._console.print(text)
            self._ensure_live()

    def pause_display(self) -> None:
        """Stop Live so the next prompt owns the terminal."""
        with self._prompt_lock:
            self._suspend_live()

    def resume_display(self) -> None:
        """Restore Live after a prompt returns the terminal."""
        with self._prompt_lock:
            self._ensure_live()

    def settle_for_prompt(self) -> None:
        """Commit the current turn before the main instruction prompt."""
        with self._prompt_lock:
            self._commit(settled=True)
            self._console.print()

    def show_blank(self) -> None:
        self._console.print()

    def show_line(self, text: str, *, style: str | None = None) -> None:
        if style:
            self._console.print(text, style=style)
        else:
            self._console.print(text)

    def present_permission(
        self,
        *,
        tool_name: str,
        subject: str,
        risk_flags: list[str] | tuple[str, ...],
        reason: str,
        targets: list[str] | tuple[str, ...],
        choices: list[dict[str, str]] | tuple[dict[str, str], ...],
        principal: str = "",
        operation: str = "",
        grant_summary: str = "",
        preview: str = "",
        cwd: str = "",
        command: str = "",
        http_method: str = "",
        http_target: str = "",
        shell_note: str = "",
        reason_code: str = "",
        reason_params: dict | None = None,
        summary_code: str = "",
        summary_params: dict | None = None,
    ) -> None:
        reason_text = present(reason_code, reason_params or {}, fallback=reason)
        summary_text = present(summary_code, summary_params or {}, fallback=grant_summary)
        shell_text = (
            present("permission.shell_note", {"cwd": cwd}, fallback=shell_note)
            if shell_note
            else ""
        )
        with self._prompt_lock:
            self._suspend_live()
            info = Text()
            info.append(t("permission.field.tool"), style="bold")
            info.append(f"{tool_name}\n")
            if operation:
                info.append(t("permission.field.operation"), style="bold")
                info.append(f"{operation}\n")
            if subject:
                info.append(t("permission.field.target"), style="bold")
                info.append(f"{subject}\n")
            info.append(t("permission.field.reason"), style="bold")
            info.append(f"{reason_text}\n")
            info.append(t("permission.field.risk"), style="bold")
            info.append(", ".join(risk_flags))
            if summary_text:
                info.append("\n" + t("permission.field.grant"), style="bold")
                info.append(summary_text)
            if command:
                info.append("\n" + t("permission.field.command"), style="bold")
                info.append(command)
            if cwd:
                info.append("\n" + t("permission.field.directory"), style="bold")
                info.append(cwd)
            if shell_text:
                info.append("\n" + t("permission.field.boundary"), style="bold")
                info.append(shell_text)
            if http_method or http_target:
                info.append("\nHTTP: ", style="bold")
                info.append(f"{http_method} {http_target}".strip())
            if preview:
                info.append("\n" + t("permission.field.preview"), style="bold")
                info.append(preview)
            if targets:
                info.append("\n" + t("permission.field.resources"), style="bold")
                info.append("; ".join(targets))
            if principal:
                info.append("\n" + t("permission.field.principal"), style="bold")
                info.append(principal)
            for choice in choices:
                label, scope, persistence = _choice_text(choice)
                info.append(f"\n- {choice.get('id', '')}: {label} | {scope} | {persistence}")
            self._console.print()
            self._console.print(
                Panel(
                    info,
                    title=f"[bold]{t('permission.title')}[/bold]",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
            for choice in choices:
                label, scope, persistence = _choice_text(choice)
                self._console.print(
                    f"  [bold]{choice['id']}[/] {label} — {scope} ({persistence})"
                )

    def present_question(
        self,
        *,
        question: str,
        context: str = "",
        options: tuple[str, ...] = (),
    ) -> None:
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
                    title=f"[bold]{t('cli.answer_needed')}[/bold]",
                    border_style="cyan",
                    padding=(1, 2),
                )
            )

    def show_submitted_input(self, text: str, *, queued: bool = False) -> None:
        with self._prompt_lock:
            if queued:
                preview = text.replace("\n", " ")
                if len(preview) > 80:
                    preview = preview[:77] + "..."
                self._console.print(f"[dim]{t('cli.queued_preview', preview=preview)}[/]")
                return
            sys.stdout.write("\033[A\033[2K\033[A\033[2K\r")
            sys.stdout.flush()
            self._console.print(
                Panel(
                    Markdown(text),
                    title=f"[bold]{t('cli.your_question')}[/bold]",
                    title_align="left",
                    border_style="cyan",
                    padding=(1, 2),
                )
            )

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
        final_turns = [t for t in session_state.turns if t.route == "final"]
        entries = collect_history_entries(
            session_state, max_turns=None if pager else max_turns
        )
        if not entries:
            return

        total_final = len(final_turns)
        shown = len(entries)
        if pager:
            suffix = t("history.full_count", count=shown)
            title_label = t("history.full_title")
        else:
            suffix = (
                t("history.recent", count=shown)
                if total_final > shown
                else t("history.total", count=shown)
            )
            title_label = t("history.summary_title")
        sid = getattr(session_state, "session_id", "?")

        def _render_to(con: Console) -> None:
            con.print()
            con.print(
                Rule(
                    f"[dim]{title_label}  session {sid} · {suffix}[/dim]",
                    style="dim",
                )
            )

            for user_text, answer_text, attachments in entries:
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
                for index, attachment in enumerate(attachments, 1):
                    con.print(Text(
                        f"  🖼 [{index}] {attachment.filename} "
                        f"({attachment.width}×{attachment.height})",
                        style="dim cyan",
                    ))

                # 回答：pager 模式不截断；摘要模式截 300 字符
                display_answer = answer_text
                truncated = False
                if not pager and len(display_answer) > 300:
                    display_answer = display_answer[:297] + "..."
                    truncated = True
                try:
                    answer_renderable = Markdown(display_answer)
                except Exception:  # Markdown 解析失败则退回纯文本
                    answer_renderable = Text(display_answer, style="dim")

                con.print(
                    Panel(
                        answer_renderable,
                        title=f"[dim]{t('history.answer')}[/dim]",
                        title_align="left",
                        border_style="dim",
                        padding=(0, 1),
                        subtitle=f"[dim italic]{t('history.truncated')}[/dim italic]" if truncated else None,
                    )
                )

            con.print()
            if not pager:
                con.print(
                    Rule(f"[dim]{t('history.divider')}[/dim]", style="dim")
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
