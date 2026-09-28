"""Textual messages posted by the TUI renderer."""

from __future__ import annotations

from typing import Any

from textual.message import Message

from .view_models import ToolView


class StreamRefresh(Message):
    """Coalesced stream update; App reads renderer.content / reasoning."""


class DraftFreeze(Message):
    """Current assistant draft should stop accepting deltas."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ToolUpsert(Message):
    def __init__(self, tool: ToolView) -> None:
        super().__init__()
        self.tool = ToolView(
            key=tool.key,
            name=tool.name,
            arguments=tool.arguments,
            status=tool.status,
            result=tool.result,
            error=tool.error,
            output=tool.output,
        )


class FinalAnswer(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class RequestUsage(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class TaskUsage(Message):
    def __init__(self, summary: str, detail: str) -> None:
        super().__init__()
        self.summary = summary
        self.detail = detail


class TurnBegin(Message):
    pass


class SystemNotice(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class InteractionNeeded(Message):
    pass


class StatusChanged(Message):
    pass


class AgentEventNotice(Message):
    def __init__(self, data: dict[str, Any], text: str) -> None:
        super().__init__()
        self.data = data
        self.text = text
