"""Skills：磁盘上的领域流程，调用时把正文写入 tool_result。"""

from __future__ import annotations

from .registry import SkillRegistry
from .types import (
    SkillDefinition,
    SkillMeta,
    SkillNotFoundError,
    SkillStoreError,
)


def optional_skill_tools(registry: SkillRegistry):
    """目录为空或不存在时不把 skill 工具写进 system prompt。"""
    if not registry.has_skills():
        return []
    from ..tools.skill_tools import build_skill_tools

    return build_skill_tools(registry)


__all__ = [
    "SkillDefinition",
    "SkillMeta",
    "SkillNotFoundError",
    "SkillRegistry",
    "SkillStoreError",
    "optional_skill_tools",
]
