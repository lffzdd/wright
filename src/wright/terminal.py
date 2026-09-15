"""Terminal capability selection that must run before Textual is imported."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping


def needs_ime_safe_keyboard(
    platform: str,
    environment: Mapping[str, str],
) -> bool:
    """Whether this terminal needs Textual's enhanced keyboard protocol disabled."""
    is_iterm = (
        environment.get("TERM_PROGRAM") == "iTerm.app"
        or environment.get("LC_TERMINAL") == "iTerm2"
    )
    return platform == "darwin" and is_iterm


def configure_terminal(
    environment: MutableMapping[str, str] = os.environ,
    platform: str = sys.platform,
) -> None:
    """Apply terminal compatibility settings before loading the TUI package."""
    if needs_ime_safe_keyboard(platform, environment):
        environment.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")
        # iTerm's Kitty protocol interferes with the macOS input-method
        # candidate window. Use its narrower xterm compatibility driver when
        # we own the protocol choice; it reports Shift+Enter while preserving
        # ordinary control shortcuts.
        if environment["TEXTUAL_DISABLE_KITTY_KEY"] == "1":
            environment.setdefault("TEXTUAL_DRIVER", "wright.tui.driver:ItermDriver")
