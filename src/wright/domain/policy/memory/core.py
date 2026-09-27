"""Rules for changing one core-memory section.

Persona is fixed configuration. That write restriction is not a complete
defense against prompt injection. Human profile and project anchor are
correctable background, and a blank string is not a request to clear them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...model.memory.core import CoreMemory

_MODES = ("append", "replace", "clear")


class CoreMemoryUpdateError(ValueError):
    """A requested core memory mutation violates the domain policy."""


@dataclass(frozen=True)
class CoreMemoryPolicy:
    """Security rules governing core memory mutations."""

    max_section_chars: int = 1500
    immutable_sections: frozenset[str] = frozenset({"persona"})
    allowed_sections: frozenset[str] = frozenset({"human_profile", "project_anchor"})

    def scope_for(self, section: str) -> str:
        """Return the storage scope that owns a mutable section."""
        normalized = section.strip().lower()
        if normalized == "human_profile":
            return "global"
        if normalized == "project_anchor":
            return "current_project"
        raise CoreMemoryUpdateError(f"Unsupported section: {section}")

    def validate_update(
        self, section: str, content: str, mode: str = "append",
    ) -> tuple[bool, str | None]:
        """Validate one mutation before any store lock is taken.

        ``clear`` is the only way to empty a section. An empty ``append`` or
        ``replace`` is rejected, including a string that is only whitespace.
        """
        if mode not in _MODES:
            allowed = ", ".join(_MODES)
            return False, f"Invalid core memory mode '{mode}'. Allowed: {allowed}."
        normalized_section = section.strip().lower()
        if normalized_section in self.immutable_sections:
            return False, f"Modifying section '{section}' is strictly prohibited by security policy."
        if normalized_section not in self.allowed_sections:
            allowed = ", ".join(sorted(self.allowed_sections))
            return False, f"Invalid core memory section '{section}'. Allowed: {allowed}."
        if mode == "clear":
            return True, None
        trimmed = content.strip()
        if not trimmed:
            return False, "Core memory content cannot be empty."
        if len(trimmed) > self.max_section_chars:
            return (
                False,
                f"Core memory content exceeds max limit of {self.max_section_chars} characters (got {len(trimmed)}).",
            )
        return True, None

    def compose_section(self, *, section: str, current: str, content: str, mode: str) -> str:
        """Return the next text for one section, including the combined length."""
        valid, error = self.validate_update(section, content, mode)
        if not valid:
            raise CoreMemoryUpdateError(error)
        if mode == "clear":
            return ""
        new_text = content.strip()
        if mode == "append" and current:
            final_text = f"{current}\n- {new_text}"
        else:
            final_text = new_text
        if len(final_text) > self.max_section_chars:
            raise CoreMemoryUpdateError(
                "Core memory content exceeds max limit of "
                f"{self.max_section_chars} characters (got {len(final_text)})."
            )
        return final_text

    def apply_update(
        self, memory: CoreMemory, section: str, content: str, mode: str = "append",
    ) -> None:
        """Apply one section change to an in-memory view. This does not persist it."""
        normalized = section.strip().lower()
        if normalized == "human_profile":
            current = memory.human_profile
            update = memory.update_human_profile
        elif normalized == "project_anchor":
            current = memory.project_anchor
            update = memory.update_project_anchor
        else:
            raise CoreMemoryUpdateError(f"Unsupported section: {section}")
        update(self.compose_section(section=normalized, current=current, content=content, mode=mode))


__all__ = [
    "CoreMemoryPolicy",
    "CoreMemoryUpdateError",
]
