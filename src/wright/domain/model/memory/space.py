"""Memory spaces: where a memory applies and who may see it.

There is one global space for the user, one repo space per project root, and
any number of personal spaces the user names (for example "装修" or "求职").
A repo space is the coding case of a space; it is not a separate system.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

SpaceType = Literal["global", "personal", "repo"]
SPACE_TYPES = frozenset({"global", "personal", "repo"})

GLOBAL_SPACE_ID = "global"
_PROJECT_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,180}"
_PERSONAL_SLUG = r"[^\s:;=/\\]{1,60}"
SPACE_ID_RE = re.compile(rf"global|repo:{_PROJECT_ID}|personal:{_PERSONAL_SLUG}")


class SpaceError(ValueError):
    pass


def repo_space_id(project_id: str) -> str:
    if not project_id or re.fullmatch(_PROJECT_ID, project_id) is None:
        raise SpaceError("invalid project id")
    return f"repo:{project_id}"


def personal_space_id(name: str) -> str:
    slug = personal_slug(name)
    if not slug:
        raise SpaceError("personal space name is empty")
    return f"personal:{slug}"


def personal_slug(name: str) -> str:
    text = unicodedata.normalize("NFKC", str(name or "")).strip().casefold()
    text = re.sub(r"[\s:;=/\\]+", "-", text).strip("-")
    return text[:60]


def space_type_of(space_id: str) -> SpaceType:
    if space_id == GLOBAL_SPACE_ID:
        return "global"
    if space_id.startswith("repo:"):
        return "repo"
    if space_id.startswith("personal:"):
        return "personal"
    raise SpaceError(f"invalid space id: {space_id}")


def is_space_id(value: str) -> bool:
    return isinstance(value, str) and SPACE_ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class Space:
    id: str
    type: SpaceType
    name: str
    created_at: str
    # Repo spaces record the project root they were opened from.
    binding: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "created_at": self.created_at,
            "binding": self.binding,
        }


__all__ = [
    "GLOBAL_SPACE_ID",
    "SPACE_ID_RE",
    "SPACE_TYPES",
    "Space",
    "SpaceError",
    "SpaceType",
    "is_space_id",
    "personal_slug",
    "personal_space_id",
    "repo_space_id",
    "space_type_of",
]
