"""Skills application subsystem."""

from __future__ import annotations

from ...domain.model.skills import SKILL_LOADER_NAME
from .prompt import catalog_reminder

__all__ = [
    "SKILL_LOADER_NAME",
    "catalog_reminder",
]
