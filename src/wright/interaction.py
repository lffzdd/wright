"""Backward-compatibility shim. Use wright.interfaces.interaction instead."""
import sys
from .interfaces import interaction

sys.modules[__name__] = interaction
