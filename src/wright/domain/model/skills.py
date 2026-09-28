"""Skill 的磁盘元数据。目录是否已注入 transcript 属于 Session。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_SKILL_FILE_BYTES = 256_000
MAX_SKILL_BODY_CHARS = 100_000
MAX_SKILLS = 40
# Agent Skills: name is the directory name, 1–64 chars, lowercase and hyphens.
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_DESCRIPTION_CHARS = 1_024
MAX_SKILL_LICENSE_CHARS = 200
MAX_SKILL_COMPATIBILITY_CHARS = 500
MAX_CATALOG_CHARS = 2_500
MIN_CATALOG_DESC_CHARS = 20
MAX_SKILL_ID_CHARS = 64

SAFE_SKILL_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class SkillStoreError(ValueError):
    """Skill 文件非法、越界或不存在。"""


class SkillNotFoundError(SkillStoreError):
    pass


@dataclass(frozen=True)
class SkillMeta:
    """Disk identity of one skill.

    ``id`` and ``name`` are the same directory name. ``allowed_tools`` is the
    suggestion list from ``allowed-tools``; it is not an authorization grant.
    ``license``, ``compatibility``, and ``metadata`` are retained metadata.
    """

    id: str
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    path: Path
    license: str = ""
    compatibility: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def skill_root(self) -> Path:
        return self.path.parent

    def metadata_map(self) -> dict[str, object]:
        return {key: json.loads(value) for key, value in self.metadata}

    def extra_map(self) -> dict[str, object]:
        return {key: json.loads(value) for key, value in self.extra}

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
            "path": str(self.path),
            "skill_root": str(self.skill_root),
            "license": self.license,
            "compatibility": self.compatibility,
            "metadata": self.metadata_map(),
            "extra": self.extra_map(),
        }


@dataclass(frozen=True)
class SkillDefinition:
    meta: SkillMeta
    body: str
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        return self.meta.id


__all__ = [
    "MAX_CATALOG_CHARS",
    "MAX_SKILLS",
    "MAX_SKILL_BODY_CHARS",
    "MAX_SKILL_COMPATIBILITY_CHARS",
    "MAX_SKILL_DESCRIPTION_CHARS",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_ID_CHARS",
    "MAX_SKILL_LICENSE_CHARS",
    "MAX_SKILL_NAME_CHARS",
    "MIN_CATALOG_DESC_CHARS",
    "SAFE_SKILL_ID_RE",
    "SkillDefinition",
    "SkillMeta",
    "SkillNotFoundError",
    "SkillStoreError",
]
