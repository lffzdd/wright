"""Fullscreen TUI host (opt-in via ``wright --ui tui``)."""

import sys

from . import app, renderer, session_control, slash, terminal
from .app import WrightTUI, run_tui
from .renderer import TUIRenderer
from .slash import SlashCompletion, tui_command_catalog, tui_help_text
from .terminal import configure_terminal, needs_ime_safe_keyboard

__all__ = [
    "SlashCompletion",
    "TUIRenderer",
    "WrightTUI",
    "app",
    "configure_terminal",
    "needs_ime_safe_keyboard",
    "renderer",
    "run_tui",
    "session_control",
    "slash",
    "terminal",
    "tui_command_catalog",
    "tui_help_text",
]

# The iTerm compatibility driver imports POSIX-only termios through Textual.
# Windows uses Textual's standard Windows driver instead.
if sys.platform != "win32":
    from . import driver
    from .driver import ItermDriver

    __all__ += ["ItermDriver", "driver"]
