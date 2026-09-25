"""Backward-compatibility shim. Use wright.interfaces.ui_events instead."""
import sys
from .interfaces import ui_events

sys.modules[__name__] = ui_events
