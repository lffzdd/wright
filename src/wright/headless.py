"""Backward-compatibility shim. Use wright.interfaces.headless instead."""
import sys
from .interfaces import headless

sys.modules[__name__] = headless
