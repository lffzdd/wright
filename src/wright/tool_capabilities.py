"""Backward-compatibility shim. Use wright.application.tool_capabilities instead."""
import sys
from .application import tool_capabilities

sys.modules[__name__] = tool_capabilities
from .application.tool_capabilities import *
