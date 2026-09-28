"""Multiline chat composer. Enter submits; slash keys navigate completions."""

from __future__ import annotations

from typing import Any

from textual.events import Key
from textual.message import Message
from textual.widgets import TextArea


class MultilineComposer(TextArea):
    """A chat composer that grows with its content and submits on Enter."""

    _MIN_VISIBLE_ROWS = 3
    _MAX_VISIBLE_ROWS = 6
    _FRAME_ROWS = 2

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class SlashChanged(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class SlashNavigate(Message):
        def __init__(self, offset: int) -> None:
            super().__init__()
            self.offset = offset

    class SlashComplete(Message):
        pass

    class SlashDismissed(Message):
        pass

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(show_line_numbers=False, **kwargs)
        self._slash_menu_open = False

    def set_slash_menu_open(self, value: bool) -> None:
        self._slash_menu_open = value

    def on_mount(self) -> None:
        self._fit_height()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self:
            self._fit_height()
            self.post_message(self.SlashChanged(self.text))

    def _fit_height(self) -> None:
        rows = min(
            self._MAX_VISIBLE_ROWS,
            max(self._MIN_VISIBLE_ROWS, self.wrapped_document.height),
        )
        self.styles.height = rows + self._FRAME_ROWS

    def on_key(self, event: Key) -> None:
        # iTerm2's xterm modifyOtherKeys protocol represents Shift+Enter as
        # ``shift+\\r``; Textual's Kitty protocol calls it ``shift+enter``.
        if event.key in {"shift+enter", "shift+\r", "ctrl+j"}:
            event.prevent_default()
            event.stop()
            start, end = self.selection
            self.replace("\n", start, end, maintain_selection_offset=False)
            return
        if self._slash_menu_open:
            if event.key == "up":
                event.prevent_default()
                event.stop()
                self.post_message(self.SlashNavigate(-1))
                return
            if event.key == "down":
                event.prevent_default()
                event.stop()
                self.post_message(self.SlashNavigate(1))
                return
            if event.key == "tab":
                event.prevent_default()
                event.stop()
                self.post_message(self.SlashComplete())
                return
            if event.key == "escape":
                event.prevent_default()
                event.stop()
                self.post_message(self.SlashDismissed())
                return
        if event.key != "enter":
            return
        event.prevent_default()
        event.stop()
        value = self.text.strip()
        if not value:
            return
        self.clear()
        self.post_message(self.Submitted(value))
