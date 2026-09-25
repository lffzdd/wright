"""Backward compatibility shim for wright.tui.session_control."""

import sys
from ..interfaces.tui import session_control

sys.modules[__name__] = session_control
