"""Fullscreen TUI host (opt-in via ``wright --ui tui``)."""

from . import app, driver, renderer, session_control, slash, terminal
from .app import WrightTUI, run_tui
from .driver import ItermDriver
from .renderer import TUIRenderer
from .slash import SlashCompletion, tui_command_catalog, tui_help_text
from .terminal import configure_terminal, needs_ime_safe_keyboard

__all__ = [
    "ItermDriver",
    "SlashCompletion",
    "TUIRenderer",
    "WrightTUI",
    "app",
    "configure_terminal",
    "driver",
    "needs_ime_safe_keyboard",
    "renderer",
    "run_tui",
    "session_control",
    "slash",
    "terminal",
    "tui_command_catalog",
    "tui_help_text",
]
