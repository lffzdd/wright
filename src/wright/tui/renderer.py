"""Backward compatibility shim for wright.tui.renderer."""

import sys
from ..interfaces.tui import renderer

sys.modules[__name__] = renderer
