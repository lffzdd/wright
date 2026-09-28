"""Skills application subsystem."""

from __future__ import annotations

from .prompt import catalog_reminder
from .registry import SkillRegistry

# Stable tool name shared by the loader adapter and request disclosure.
# Schema exposure is separate from registration.
SKILL_LOADER_NAME = "load_skill"

__all__ = [
    "SKILL_LOADER_NAME",
    "SkillRegistry",
    "catalog_reminder",
]
