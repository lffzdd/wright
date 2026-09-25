"""Backward compatibility shim for wright.tui.slash."""

import sys
from ..interfaces.tui import slash

sys.modules[__name__] = slash
