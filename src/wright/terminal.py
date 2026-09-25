"""Backward-compatibility shim. Use wright.tui.terminal instead."""
from .tui.terminal import configure_terminal, needs_ime_safe_keyboard

__all__ = ["configure_terminal", "needs_ime_safe_keyboard"]
