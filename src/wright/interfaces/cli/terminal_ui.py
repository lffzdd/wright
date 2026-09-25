"""Terminal UI rendering for interactive CLI sessions."""

from __future__ import annotations

from ..renderer import ConsoleRenderer, Renderer, SilentRenderer

# Canonical TerminalUI represents the primary terminal rendering interface
TerminalUI = ConsoleRenderer

__all__ = [
    "ConsoleRenderer",
    "Renderer",
    "SilentRenderer",
    "TerminalUI",
]
