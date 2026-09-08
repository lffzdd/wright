"""Fullscreen TUI host (opt-in via ``wright --ui tui``)."""

from .app import WrightTUI, run_tui
from .renderer import TUIRenderer

__all__ = ["TUIRenderer", "WrightTUI", "run_tui"]
