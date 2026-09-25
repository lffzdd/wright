"""Backward-compatibility shim. Use wright.domain.prompt instead."""
import sys
from .domain import prompt

sys.modules[__name__] = prompt
