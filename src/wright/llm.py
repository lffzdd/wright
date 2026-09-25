"""Backward-compatibility shim. Use wright.infrastructure.llm.llm instead."""
import sys
from .infrastructure.llm import llm

sys.modules[__name__] = llm
