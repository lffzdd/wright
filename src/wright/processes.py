"""Backward-compatibility shim. Use wright.core.processes instead."""
import sys
from .core import processes

sys.modules[__name__] = processes
