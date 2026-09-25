"""Backward-compatibility facade for FactPolicy (migrated to .memory.fact)."""

from __future__ import annotations

from .memory.fact import FactPolicy, is_safe_fact

__all__ = ["FactPolicy", "is_safe_fact"]
