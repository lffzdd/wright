"""The skill lookups the load_skill tool needs."""

from __future__ import annotations

from typing import Protocol

from ..model.skills import SkillDefinition


class ISkillCatalog(Protocol):
    def has_skills(self) -> bool: ...

    def get(self, skill_id: str) -> SkillDefinition: ...
