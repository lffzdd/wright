"""Backward-compatibility shim. Use wright.domain.capabilities instead."""
import sys
from .domain import capabilities

sys.modules[__name__] = capabilities
