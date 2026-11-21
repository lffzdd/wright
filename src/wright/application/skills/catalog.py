"""The skill catalog with the skills turned off in settings left out."""

from __future__ import annotations

from collections.abc import Callable, Collection

from ...domain.gateway.skills import ISkillCatalog
from ...domain.model.settings import SettingsError
from ...domain.model.skills import SkillDefinition, SkillMeta, SkillNotFoundError


class EnabledSkills:
    """An ``ISkillCatalog`` that hides disabled skills from the model.

    ``disabled`` is asked on every lookup, so a switch flipped in settings
    applies from the next request. ``has_skills`` still answers for every
    skill on disk: the loader's schema stays in the tool list, which keeps the
    prompt cache, and only the catalog reminder at the end of the request
    changes.
    """

    def __init__(
        self,
        inner: ISkillCatalog,
        disabled: Callable[[], Collection[str]],
        write: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self.inner = inner
        self._disabled = disabled
        self._write = write

    def disabled(self) -> frozenset[str]:
        return frozenset(self._disabled())

    def has_skills(self) -> bool:
        return self.inner.has_skills()

    def list_metas(self, query: str = "") -> list[SkillMeta]:
        off = self.disabled()
        return [meta for meta in self.inner.list_metas(query) if meta.id not in off]

    def all_metas(self) -> list[SkillMeta]:
        """Every skill on disk, the disabled ones included, for the switches."""
        return self.inner.list_metas()

    def set_enabled(self, skill_id: str, enabled: bool) -> None:
        """Turn one skill on or off by rewriting the disabled list.

        Raises ``SkillNotFoundError`` for an id not on disk; the writer may
        raise ``SettingsError`` when the list cannot be saved.
        """
        if not any(meta.id == skill_id for meta in self.all_metas()):
            raise SkillNotFoundError(f"Unknown skill: {skill_id}")
        if self._write is None:
            raise SettingsError("skill switches cannot be saved here")
        current = list(dict.fromkeys(self._disabled()))
        if enabled:
            updated = [item for item in current if item != skill_id]
        else:
            updated = current if skill_id in current else [*current, skill_id]
        if updated != current:
            self._write(tuple(updated))

    def get(self, skill_id: str) -> SkillDefinition:
        definition = self.inner.get(skill_id)
        if definition.id in self.disabled():
            raise SkillNotFoundError(
                f"Skill {definition.id} is turned off (skills.disabled); "
                "ask the user to turn it on before using it."
            )
        return definition

    def scan_errors(self) -> tuple[str, ...]:
        return self.inner.scan_errors()

    def rows(self) -> list[dict]:
        """Every skill on disk and whether the model is offered it."""
        off = self.disabled()
        return [
            {"id": meta.id, "name": meta.name, "description": meta.description, "enabled": meta.id not in off}
            for meta in sorted(self.all_metas(), key=lambda meta: meta.id)
        ]


__all__ = ["EnabledSkills"]
