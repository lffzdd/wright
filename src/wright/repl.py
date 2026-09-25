"""Backward-compatibility shim. Use wright.interfaces.repl instead."""
import sys
from .interfaces import repl

sys.modules[__name__] = repl
