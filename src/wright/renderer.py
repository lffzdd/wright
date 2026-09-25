"""Backward-compatibility shim. Use wright.interfaces.renderer instead."""
import sys
from .interfaces import renderer

sys.modules[__name__] = renderer
