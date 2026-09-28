"""Text formatting for the fullscreen transcript.

These functions turn session and tool values into Rich renderables. They do
not own widgets or the application event loop.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Group
from rich.json import JSON as RichJSON
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text

from ..i18n import present, t
from .view_models import ToolView

_COMMAND_OUTPUT_LINES = 24

def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)

def _context_ring(tokens: int | None, limit: int | None) -> tuple[str, str, str]:
    """Format the current session context for a compact indicator and tooltip."""
    if tokens is None or not limit:
        return "○", t("tui.context.waiting"), t("tui.context.waiting_detail")
    ratio = max(0.0, min(tokens / limit, 1.0))
    ring = ("○", "◔", "◑", "◕", "●")[min(4, int(ratio * 4.999))]
    compact = f"{ring}  {ratio:.0%}"
    detail = (
        f"Context window:\n{ratio:.0%} full\n"
        f"{tokens:,} / {limit:,} tokens used\n\n"
        "Current session context"
    )
    return compact, detail, "warning" if ratio >= 0.75 else "normal"

def _short_tokens(tokens: int) -> str:
    return f"{tokens / 1_000:.1f}k" if tokens >= 1_000 else str(tokens)

def _task_usage_detail(prompt: int, completion: int, total: int) -> str:
    return (
        "Task usage:\n"
        f"{prompt:,} in\n"
        f"{completion:,} out\n"
        f"{total:,} total"
    )

def _format_assistant_text(text: str, *, draft: bool) -> Any:
    if not text:
        return "…" if draft else ""
    if draft:
        return text
    try:
        return RichMarkdown(text)
    except Exception:
        return text

def _tool_class(tool: ToolView) -> str:
    if tool.status == "error":
        return "error"
    if tool.status == "done":
        return "done"
    if tool.status == "awaiting_approval":
        return "awaiting"
    return "running"

def _tool_arg_summary(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        if isinstance(args, str) and args.strip():
            return args.strip()[:36]
        return ""
    if "command" in args and isinstance(args["command"], str):
        cmd = args["command"].strip().replace("\n", " ")
        return f"$ {cmd[:36]}…" if len(cmd) > 36 else f"$ {cmd}"
    if name == "edit_file" and isinstance(args.get("file"), str):
        return args["file"]
    for key in ("path", "file_path", "file", "TargetFile", "AbsolutePath", "SearchDirectory"):
        if key in args and isinstance(args[key], str):
            p = args[key].strip()
            parts = p.split("/")
            return "/".join(parts[-2:]) if len(parts) > 2 else p
    for key in ("query", "Query", "pattern", "Pattern"):
        if key in args and isinstance(args[key], str):
            q = args[key].strip()
            return f'"{q[:30]}…"' if len(q) > 30 else f'"{q}"'
    for key in ("task", "instruction", "Instruction", "prompt"):
        if key in args and isinstance(args[key], str):
            s = args[key].strip().replace("\n", " ")
            return s[:36] + "…" if len(s) > 36 else s
    for v in args.values():
        if isinstance(v, str) and v.strip():
            s = v.strip().replace("\n", " ")
            return s[:32] + "…" if len(s) > 32 else s
    return ""

def _tool_title(tool: ToolView) -> str:
    icon = {
        "planned": "○",
        "awaiting_approval": "⚠",
        "running": "⏳",
        "done": "✓",
        "error": "✗",
    }.get(tool.status, "○")
    summary = _tool_arg_summary(tool.name, tool.arguments)
    if summary:
        return f"{icon} {tool.name} · {summary}"
    return f"{icon} {tool.name} · {tool.status}"

def _format_diff(diff_text: str) -> Text:
    t = Text()
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            t.append(line + "\n", style="green")
        elif line.startswith("-") and not line.startswith("---"):
            t.append(line + "\n", style="red")
        elif line.startswith("@@"):
            t.append(line + "\n", style="cyan")
        else:
            t.append(line + "\n", style="dim")
    return t

def _tool_body(tool: ToolView) -> Any:
    parts: list[Any] = []
    args = tool.arguments
    if args:
        if isinstance(args, dict) and "command" in args and isinstance(args["command"], str):
            parts.append(Text(f"$ {args['command']}", style="bold cyan"))
        elif isinstance(args, dict) and tool.name == "edit_file":
            old_text = str(args.get("old_text") or "")
            new_text = str(args.get("new_text") or "")
            diff = "\n".join(
                [
                    *(f"-{line}" for line in old_text.splitlines() or [""]),
                    *(f"+{line}" for line in new_text.splitlines() or [""]),
                ]
            )
            if old_text or new_text:
                parts.append(_format_diff(diff))
            else:
                parts.append(Text("(no changes)", style="dim italic"))
        else:
            try:
                parts.append(Group(Text(t("tool.arguments"), style="bold dim"), RichJSON.from_data(args)))
            except Exception:
                parts.append(Text(_json_text(args), style="dim"))

    if tool.output:
        lines = tool.output.splitlines()
        clipped = lines[-_COMMAND_OUTPUT_LINES:]
        prefix = "" if len(lines) <= _COMMAND_OUTPUT_LINES else "…\n"
        body_txt = prefix + "\n".join(clipped)
        if any(l.startswith("@@") or (l.startswith("+") and not l.startswith("+++")) or (l.startswith("-") and not l.startswith("---")) for l in clipped):
            parts.append(_format_diff(body_txt))
        else:
            parts.append(Text(body_txt, style="dim"))

    if tool.status == "error" and tool.error:
        parts.append(Text(
            present(tool.display_code, tool.display_params, fallback=t("tool.error_line", error=tool.error)),
            style="bold red",
        ))
    elif tool.result is not None:
        if isinstance(tool.result, (dict, list)):
            try:
                parts.append(Group(Text(t("tool.result"), style="bold dim"), RichJSON.from_data(tool.result)))
            except Exception:
                parts.append(Text(_json_text(tool.result), style="dim"))
        else:
            parts.append(Text(str(tool.result), style="dim"))

    if not parts:
        return Text(t("tool.no_payload"), style="dim italic")
    if len(parts) == 1:
        return parts[0]
    return Group(*parts)
