"""Backward-compatibility shim. Use wright.domain.tool_protocol instead."""
import sys
from .domain import tool_protocol

sys.modules[__name__] = tool_protocol
