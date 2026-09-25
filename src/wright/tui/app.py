"""Backward compatibility shim for wright.tui.app."""

import sys
from ..interfaces.tui import app

sys.modules[__name__] = app
