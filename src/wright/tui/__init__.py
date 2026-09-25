"""Backward compatibility shim for wright.tui."""

import sys
from ..interfaces import tui

sys.modules[__name__] = tui
sys.modules[f"{__name__}.app"] = tui.app
sys.modules[f"{__name__}.driver"] = tui.driver
sys.modules[f"{__name__}.renderer"] = tui.renderer
sys.modules[f"{__name__}.session_control"] = tui.session_control
sys.modules[f"{__name__}.slash"] = tui.slash
sys.modules[f"{__name__}.terminal"] = tui.terminal

from ..interfaces.tui import TUIRenderer, WrightTUI, run_tui

__all__ = ["TUIRenderer", "WrightTUI", "run_tui"]
