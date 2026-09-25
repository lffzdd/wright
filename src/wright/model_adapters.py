"""Backward-compatibility shim. Use wright.infrastructure.llm.model_adapters instead."""
import sys
from .infrastructure.llm import model_adapters

sys.modules[__name__] = model_adapters
