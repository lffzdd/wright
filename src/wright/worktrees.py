"""Backward-compatibility shim. Use wright.infrastructure.workspace.worktrees instead."""
import sys
from .infrastructure.workspace import worktrees

sys.modules[__name__] = worktrees
