"""Skills application subsystem."""

from __future__ import annotations

from .prompt import catalog_reminder
from .registry import SkillRegistry

__all__ = [
    "SkillRegistry",
    "catalog_reminder",
]
