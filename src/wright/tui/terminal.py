"""Backward compatibility shim for wright.tui.terminal."""

import sys
from ..interfaces.tui import terminal

sys.modules[__name__] = terminal
