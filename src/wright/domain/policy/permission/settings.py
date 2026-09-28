"""Parsed permission rules. Persistence of the settings file is not this module."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Literal

PermissionMode = Literal["default", "acceptEdits", "bypass", "plan"]
_VALID_MODES = ("default", "acceptEdits", "bypass", "plan")


@dataclass(frozen=True)
class PermissionRule:
    """一条解析后的规则:工具名 +(可选)主体 glob。无 glob 即匹配该工具任意调用。"""

    tool_name: str
    subject_glob: str | None = None

    @classmethod
    def parse(cls, raw: str) -> PermissionRule:
        raw = raw.strip()
        if raw.endswith(")") and "(" in raw:
            name, _, rest = raw.partition("(")
            return cls(name.strip(), rest[:-1].strip() or None)
        return cls(raw, None)

    def matches(self, tool_name: str, subject: str) -> bool:
        if self.tool_name != tool_name:
            return False
        if self.subject_glob is None:
            return True
        # A prefix glob is not a shell parser.  Without this guard a rule such as
        # execute_command(ls*) also matches `ls; rm -rf ...` or `ls $(...)`.
        # Scoped shell rules deliberately cover one simple command only.
        if tool_name == "execute_command" and _has_shell_composition(subject):
            return False
        return fnmatch(subject, self.subject_glob)


@dataclass
class PermissionSettings:
    """一份权限配置:模式 + allow/deny/ask 规则。

    三张表的关系:deny 最强(永远拒),ask 次之(强制询问,压过 allow 与各模式的自动放行),
    allow 最弱(自动放行)。ask 的用处是"在一片宽 allow 里挖个洞"。
    """

    mode: PermissionMode = "default"
    allow: list[PermissionRule] = field(default_factory=list)
    deny: list[PermissionRule] = field(default_factory=list)
    ask: list[PermissionRule] = field(default_factory=list)
    additional_directories: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> PermissionSettings:
        mode = data.get("mode", "default")
        if mode not in _VALID_MODES:
            raise ValueError(
                f"未知权限模式 {mode!r},可选: {', '.join(_VALID_MODES)}"
            )
        perms = data.get("permissions", {}) or {}
        return cls(
            mode=mode,
            allow=[PermissionRule.parse(r) for r in perms.get("allow", [])],
            deny=[PermissionRule.parse(r) for r in perms.get("deny", [])],
            ask=[PermissionRule.parse(r) for r in perms.get("ask", [])],
            additional_directories=[
                str(item) for item in perms.get("additionalDirectories", [])
                if isinstance(item, str) and item.strip()
            ],
        )


def _has_shell_composition(command: str) -> bool:
    """Conservatively reject compound/dynamic shell syntax in scoped allows."""
    return bool(
        re.search(r"(?:\n|\r|&&|\|\||[;|`]|\$\(|[<>]\(|(?:^|\s)[0-9&]*>{1,2})", command)
    )
