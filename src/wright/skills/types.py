"""Skill 的磁盘元数据。目录是否已注入 transcript 属于 SessionState。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re


MAX_SKILL_FILE_BYTES = 64_000
MAX_SKILL_BODY_CHARS = 8_000
MAX_SKILLS = 40
MAX_SKILL_NAME_CHARS = 80
MAX_SKILL_DESCRIPTION_CHARS = 400
MAX_CATALOG_CHARS = 2_500
MIN_CATALOG_DESC_CHARS = 20
MAX_SKILL_ID_CHARS = 80

SAFE_SKILL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")


class SkillStoreError(ValueError):
    """Skill 文件非法、越界或不存在。"""


class SkillNotFoundError(SkillStoreError):
    pass


@dataclass(frozen=True)
class SkillMeta:
    id: str
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    path: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
            "path": str(self.path),
        }


@dataclass(frozen=True)
class SkillDefinition:
    meta: SkillMeta
    body: str
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        return self.meta.id
