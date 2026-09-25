"""Backward-compatibility shim. Use wright.infrastructure.storage.artifacts instead."""
import sys
from .infrastructure.storage import artifacts

sys.modules[__name__] = artifacts
