"""Backward compatibility shim for wright.web.runtime_manager."""

import sys
from ..interfaces.web import runtime_manager

sys.modules[__name__] = runtime_manager
