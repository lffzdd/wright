"""Core memory policy (Physical anti-tampering and token explosion guardrails)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoreMemoryPolicy:
    """Security rules governing core memory mutations."""

    max_section_chars: int = 1500
    immutable_sections: frozenset[str] = frozenset({"persona"})
    allowed_sections: frozenset[str] = frozenset({"human_profile", "project_anchor"})

    def validate_update(self, section: str, content: str) -> tuple[bool, str | None]:
        """Validate whether an update to a core memory section is permissible.

        Rules:
        1. Agent Persona is immutable to prevent prompt injection / hypnosis attacks.
        2. Section must be one of the explicitly allowed sections (human_profile, project_anchor).
        3. Content length must strictly respect max_section_chars to avoid context bloat.
        4. Content cannot be blank.
        """
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


__all__ = ["CoreMemoryPolicy"]
