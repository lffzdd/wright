"""Backward-compatibility shim. Use wright.domain.model instead."""
import sys
from .domain import model

sys.modules[__name__] = model
