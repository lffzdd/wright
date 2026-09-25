"""Backward-compatibility shim. Use wright.infrastructure.workspace.project instead."""
import sys
from .infrastructure.workspace import project

sys.modules[__name__] = project
