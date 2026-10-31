"""The skill lookups the load_skill tool needs."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..model.skills import SkillDefinition, SkillMeta


class ISkillCatalog(Protocol):
    def has_skills(self) -> bool: ...

    def get(self, skill_id: str) -> SkillDefinition: ...

    def list_metas(self, query: str = "") -> list[SkillMeta]: ...

    def scan_errors(self) -> tuple[str, ...]: ...


class ISkillStore(Protocol):
    """Skill files in one directory, for tools that manage them."""

    def normalize_id(self, skill_id: str) -> str: ...

    def file_path(self, directory: Path, skill_id: str) -> Path: ...

    def load(self, path: Path, skill_id: str | None = None) -> SkillDefinition: ...

    def scan(self, directory: Path) -> tuple[list[SkillDefinition], list[str]]: ...

    def write(self, directory: Path, skill_id: str, *, name: str, description: str, body: str, allowed_tools: list[str] | None = None) -> Path: ...
