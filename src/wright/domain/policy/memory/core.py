"""Core memory policy (Physical anti-tampering and token explosion guardrails)."""

from __future__ import annotations

from dataclasses import dataclass

from ...model.memory import CoreMemory


class CoreMemoryUpdateError(ValueError):
    """A requested core memory mutation violates the domain policy."""


@dataclass(frozen=True)
class CoreMemoryPolicy:
    """Security rules governing core memory mutations."""

    max_section_chars: int = 1500
    immutable_sections: frozenset[str] = frozenset({"persona"})
    allowed_sections: frozenset[str] = frozenset({"human_profile", "project_anchor"})

    def validate_update(
        self, section: str, content: str, mode: str = "append",
    ) -> tuple[bool, str | None]:
        """Validate whether an update to a core memory section is permissible.

        Rules:
        1. Agent Persona is immutable to prevent prompt injection / hypnosis attacks.
        2. Section must be one of the explicitly allowed sections (human_profile, project_anchor).
        3. Content length must strictly respect max_section_chars to avoid context bloat.
        4. Content cannot be blank.
        5. Mode must be exactly append or replace.
        """
        if mode not in ("append", "replace"):
            return False, f"Invalid core memory mode '{mode}'. Allowed: append, replace."
        normalized_section = section.strip().lower()
        if normalized_section in self.immutable_sections:
            return False, f"Modifying section '{section}' is strictly prohibited by security policy."
        if normalized_section not in self.allowed_sections:
            allowed = ", ".join(sorted(self.allowed_sections))
            return False, f"Invalid core memory section '{section}'. Allowed: {allowed}."
        trimmed = content.strip()
        if not trimmed:
            return False, "Core memory content cannot be empty."
        if len(trimmed) > self.max_section_chars:
            return (
                False,
                f"Core memory content exceeds max limit of {self.max_section_chars} characters (got {len(trimmed)}).",
            )
        return True, None

    def apply_update(
        self, memory: CoreMemory, section: str, content: str, mode: str = "append",
    ) -> None:
        """Validate against the current snapshot before changing the entity."""
        valid, error = self.validate_update(section, content, mode)
        if not valid:
            raise CoreMemoryUpdateError(error)
        normalized = section.strip().lower()
        if normalized == "human_profile":
            current = memory.human_profile
            update = memory.update_human_profile
        elif normalized == "project_anchor":
            current = memory.project_anchor
            update = memory.update_project_anchor
        else:
            raise CoreMemoryUpdateError(f"Unsupported section: {section}")
        new_text = content.strip()
        final_text = f"{current}\n- {new_text}" if mode == "append" and current else new_text
        valid, error = self.validate_update(normalized, final_text, mode)
        if not valid:
            raise CoreMemoryUpdateError(error)
        update(final_text)


__all__ = ["CoreMemoryPolicy", "CoreMemoryUpdateError"]
