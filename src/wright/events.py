"""Backward-compatibility shim. Use wright.domain.events instead."""
import sys
from .domain import events

sys.modules[__name__] = events
