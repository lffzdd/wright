"""Backward compatibility shim for wright.tui.driver."""

import sys
from ..interfaces.tui import driver

sys.modules[__name__] = driver
