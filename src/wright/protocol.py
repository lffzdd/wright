"""Backward-compatibility shim. Use wright.domain.protocol instead."""
import sys
from .domain import protocol

sys.modules[__name__] = protocol
