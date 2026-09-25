"""Core memory domain entity (The permanent pinned sticky note on the agent's forehead)."""

from __future__ import annotations

from dataclasses import dataclass

from ...base.base_entity import BaseEntity

DEFAULT_PERSONA = (
    "I am an expert architectural coding assistant. I strictly follow Domain-Driven Design (DDD) "
    "principles, prioritize high cohesion and loose coupling, and always verify changes with tests."
)
DEFAULT_HUMAN_PROFILE = (
    "User is a senior software architect working in macOS. "
    "Prefers concise answers, elegant modular architecture, and zero regressions."
)
DEFAULT_PROJECT_ANCHOR = (
    "Project is 'wright', located under src/wright. Uses uv and pytest. "
    "Core invariant: Domain layer must remain pure with zero external I/O."
)


@dataclass(eq=False)
class CoreMemory(BaseEntity):
    """Core memory entity pinned permanently in System Prompt."""

    persona: str = DEFAULT_PERSONA
    human_profile: str = DEFAULT_HUMAN_PROFILE
    project_anchor: str = DEFAULT_PROJECT_ANCHOR

    def update_human_profile(self, new_profile: str) -> None:
        self.human_profile = new_profile.strip()
        self.mark_updated()

    def update_project_anchor(self, new_anchor: str) -> None:
        self.project_anchor = new_anchor.strip()
        self.mark_updated()

    def render_block(self) -> str:
        """Render English formatted core memory block to pin on top of System Prompt."""
        return (
            "<CORE_MEMORY>\n"
            f"- Persona: {self.persona}\n"
            f"- Human Profile: {self.human_profile}\n"
            f"- Project Core Anchor: {self.project_anchor}\n"
            "</CORE_MEMORY>"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "persona": self.persona,
            "human_profile": self.human_profile,
            "project_anchor": self.project_anchor,
            "updated_at": str(self.updated_at),
        }


__all__ = [
    "DEFAULT_HUMAN_PROFILE",
    "DEFAULT_PERSONA",
    "DEFAULT_PROJECT_ANCHOR",
    "CoreMemory",
]
