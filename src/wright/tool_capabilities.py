"""Backward-compatibility shim. Use wright.app.tool_capabilities instead."""
import sys
from .app import tool_capabilities

sys.modules[__name__] = tool_capabilities
