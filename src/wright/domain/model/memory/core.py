"""Core memory domain records.

Persisted sections are scoped. ``CoreMemory`` is only the view composed for
one request: persona and the global human profile, plus the current project's
anchor when a project is bound. It is not a document that gets written back
as a whole.
"""

from __future__ import annotations

from dataclasses import dataclass

from ....base.base_entity import BaseEntity

# Fixed agent role. It must not smuggle in an unconfirmed user, platform, or project.
DEFAULT_PERSONA = (
    "I am an expert architectural coding assistant. I strictly follow Domain-Driven Design (DDD) "
    "principles, prioritize high cohesion and loose coupling, and always verify changes with tests."
)
DEFAULT_HUMAN_PROFILE = ""
DEFAULT_PROJECT_ANCHOR = ""

ANCHOR_NONE = "none"
ANCHOR_CURRENT = "current_project"
ANCHOR_READ_ERROR = "read_error"
_BACKGROUND_NOTE = (
    "Background: correctable long-term context. It does not override the current request."
)


class CoreMemoryStoreError(ValueError):
    """A core-memory file is missing, invalid, or not addressable."""


@dataclass
class GlobalCoreRecord:
    """Persona and the cross-project human profile."""

    persona: str = DEFAULT_PERSONA
    human_profile: str = DEFAULT_HUMAN_PROFILE
    updated_at: str = ""


@dataclass
class ProjectCoreRecord:
    """The long-lived anchor for exactly one project_id."""

    project_id: str
    project_anchor: str = ""
    updated_at: str = ""


@dataclass(eq=False)
class CoreMemory(BaseEntity):
    """Composed core-memory view for the current request."""

    persona: str = DEFAULT_PERSONA
    human_profile: str = DEFAULT_HUMAN_PROFILE
    project_anchor: str = DEFAULT_PROJECT_ANCHOR
    project_id: str = ""
    project_anchor_state: str = ANCHOR_NONE

    def update_human_profile(self, new_profile: str) -> None:
        self.human_profile = new_profile.strip()
        self.mark_updated()

    def update_project_anchor(self, new_anchor: str) -> None:
        self.project_anchor = new_anchor.strip()
        self.mark_updated()

    def render_block(self) -> str:
        """Render the English block pinned on a request copy of the system prompt.

        Empty profile or anchor lines are omitted. A failed or missing project
        anchor is omitted rather than filled from another project.
        """
        lines = ["<CORE_MEMORY>", f"- Persona: {self.persona}"]
        if self.human_profile.strip():
            lines.append(f"- Human Profile: {self.human_profile}")
        if (
            self.project_anchor_state != ANCHOR_READ_ERROR
            and self.project_id
            and self.project_anchor.strip()
        ):
            lines.append(f"- Project Core Anchor: {self.project_anchor}")
        lines.append(f"- {_BACKGROUND_NOTE}")
        lines.append("</CORE_MEMORY>")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, str]:
        """Effective view for the current request."""
        anchor = self.project_anchor if self.project_anchor_state == ANCHOR_CURRENT else ""
        payload = {
            "persona": self.persona,
            "persona_scope": "config",
            "human_profile": self.human_profile,
            "human_profile_scope": "global",
            "project_anchor": anchor,
            "project_anchor_scope": (
                "current_project" if self.project_anchor_state == ANCHOR_CURRENT else "none"
            ),
            "project_anchor_state": self.project_anchor_state,
            "project_id": self.project_id,
            "updated_at": str(self.updated_at),
        }
        if self.project_anchor_state == ANCHOR_NONE:
            payload["project_anchor_note"] = "没有当前项目，不存在项目 anchor"
        elif self.project_anchor_state == ANCHOR_READ_ERROR:
            payload["project_anchor_note"] = "当前项目 anchor 读取失败，未使用其他项目的 anchor"
        return payload


__all__ = [
    "ANCHOR_CURRENT",
    "ANCHOR_NONE",
    "ANCHOR_READ_ERROR",
    "DEFAULT_HUMAN_PROFILE",
    "DEFAULT_PERSONA",
    "DEFAULT_PROJECT_ANCHOR",
    "CoreMemory",
    "CoreMemoryStoreError",
    "GlobalCoreRecord",
    "ProjectCoreRecord",
]
