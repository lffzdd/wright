"""iTerm2 keyboard compatibility for Wright's Textual application.

The Kitty keyboard protocol makes Shift+Enter distinguishable, but can break
the macOS input-method candidate window in iTerm2. xterm's ``modifyOtherKeys``
level 1 reports Shift+Enter while preserving ordinary control shortcuts such
as Ctrl+C.
"""

from __future__ import annotations

from typing import cast

from textual._ansi_sequences import ANSI_SEQUENCES_KEYS
from textual.drivers.linux_driver import LinuxDriver
from textual.keys import Keys

_SHIFT_ENTER = "\x1b[27;2;13~"
_ENABLE_MODIFY_OTHER_KEYS = "\x1b[>4;1m"
_RESET_MODIFY_OTHER_KEYS = "\x1b[>4m"

# Textual recognizes this sequence through its Kitty parser only. The iTerm
# compatibility path intentionally disables that parser for macOS IMEs, so add
# the single xterm sequence it needs to the ordinary ANSI table instead.
_key_sequences = cast(dict[str, tuple[Keys, ...]], ANSI_SEQUENCES_KEYS)
_key_sequences.setdefault(_SHIFT_ENTER, (Keys.ControlJ,))


class ItermDriver(LinuxDriver):
    """Keep iTerm2 IME input working while recognizing Shift+Enter."""

    def start_application_mode(self) -> None:
        super().start_application_mode()
        self.write(_ENABLE_MODIFY_OTHER_KEYS)
        self.flush()

    def stop_application_mode(self) -> None:
        # Restore iTerm's per-session key mode before returning to the shell.
        self.write(_RESET_MODIFY_OTHER_KEYS)
        self.flush()
        super().stop_application_mode()
