"""Backward compatibility shim for wright.web."""

import sys
from ..interfaces import web

sys.modules[__name__] = web
sys.modules[f"{__name__}.auth"] = web.auth
sys.modules[f"{__name__}.diff"] = web.diff
sys.modules[f"{__name__}.runtime_manager"] = web.runtime_manager
sys.modules[f"{__name__}.server"] = web.server

from ..interfaces.web import run_web

__all__ = ["run_web"]
