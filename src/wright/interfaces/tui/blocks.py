"""Transcript widgets for messages, reasoning, and tool cards."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.widgets import Collapsible, Static

from .format import _format_assistant_text, _tool_body, _tool_class, _tool_title
from .view_models import ToolView


class UserBlock(Static):
    def __init__(self, text: str) -> None:
        content = Text()
        content.append("❯ ", style="bold cyan")
        content.append(text)
        super().__init__(content, classes="msg user")

class AssistantBlock(Static):
    def __init__(self, text: str = "", *, draft: bool = False) -> None:
        classes = "msg assistant draft" if draft else "msg assistant"
        super().__init__(_format_assistant_text(text, draft=draft), classes=classes)

    def set_draft(self, text: str) -> None:
        self.set_classes("msg assistant draft")
        self.update(_format_assistant_text(text, draft=True))

    def set_final(self, text: str) -> None:
        self.set_classes("msg assistant")
        self.update(_format_assistant_text(text, draft=False))

class ReasoningBlock(Collapsible):
    """A visible, in-place view of the model's streamed reasoning."""

    def __init__(self, text: str = "", *, collapsed: bool = False) -> None:
        self._body = Static(text or "…", classes="reasoning-body")
        super().__init__(
            self._body,
            title="思考" if text else "思考中",
            collapsed=collapsed,
            classes="reasoning",
        )

    def update_reasoning(self, text: str) -> None:
        self._body.update(text or "…")

class SubagentBlock(Static):
    def __init__(self, data: dict[str, Any], text: str) -> None:
        status = str(data.get("status", "unknown"))
        depth = data.get("depth", "?")
        task_id = str(data.get("task_id", "?"))[:8]
        task = str(data.get("task", ""))
        if len(task) > 80:
            task = task[:77] + "…"

        line = Text()
        line.append("🤖 SubAgent ", style="bold magenta")
        line.append(f"[{task_id}] ", style="bold white")
        line.append(f"d{depth} ", style="dim")
        status_style = {
            "running": "cyan bold",
            "completed": "green bold",
            "failed": "red bold",
        }.get(status, "yellow")
        line.append(f"· {status}", style=status_style)
        if task:
            line.append(f"  {task}", style="dim")
        super().__init__(line, classes=f"subagent {status}")

class SystemBlock(Static):
    def __init__(self, text: str) -> None:
        super().__init__(text, classes="msg system")

class UsageBlock(Static):
    def __init__(self, text: str, *, total: bool = False) -> None:
        classes = "usage total" if total else "usage"
        super().__init__(text, classes=classes)

class TaskUsageBlock(UsageBlock):
    def __init__(self, summary: str, detail: str) -> None:
        super().__init__(summary, total=True)
        self.tooltip = detail

class ToolBlock(Collapsible):
    def __init__(self, tool: ToolView) -> None:
        self._body = Static(_tool_body(tool), classes="tool-body")
        super().__init__(
            self._body,
            title=_tool_title(tool),
            collapsed=True,
            classes=f"tool {_tool_class(tool)}",
        )
        self.tool_key = tool.key

    def apply(self, tool: ToolView) -> None:
        self.tool_key = tool.key
        self.title = _tool_title(tool)
        self.set_classes(f"tool {_tool_class(tool)}")
        self._body.update(_tool_body(tool))
