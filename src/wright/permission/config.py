"""规则式权限裁决:把 ask 交给一份「权限模式 + allow/deny 规则」的配置来自动判定。

为什么要这一层
--------------
Tool.describe_access 只描述工具本次可能涉及的操作——它不知道"这次该不该放行",那是策略问题。
`PermissionResolver` 直接消费本模块提供的持久化配置，不把规则判断放在可被遗漏的普通审批
handler 中。

判定顺序(fail-closed:拿不准就拒)
----------------------------------
1. deny 规则命中 → 拒(deny 永远压过 allow)。
2. 权限模式特判:
   - bypass     → 放行(只受 deny 约束),给可信无人值守。
   - plan       → 拒一切有副作用的调用(能走到这里的本就都带副作用)。
   - acceptEdits→ 风险只涉及读写本地文件(无 shell/网络)时自动放行。
3. allow 规则命中 → 放行。
4. 都没命中 → 拒,并在 reason 里说明"没有匹配的 allow 规则"。

规则字符串语法
--------------
- `"write_file"`                         裸工具名:匹配该工具的任意调用。
- `"execute_command(git status*)"`       带括号:对该工具的"主体串"做 fnmatch。
- `"write_file(drafts/*.md)"`            文件类工具主体是 file/directory 参数。
- `"http_request(https://api.example.com/*)"` 网络工具主体是 url 参数。

主体串按工具取最有判别力的那个参数(命令取 command、文件取 file/directory、网络取
url),取不到就退化成参数的 JSON,保证任何工具都能写规则。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

PermissionMode = Literal["default", "acceptEdits", "bypass", "plan"]
_VALID_MODES = ("default", "acceptEdits", "bypass", "plan")

_CONFIG_LOCK = threading.RLock()


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
    """一份权限配置:模式 + allow/deny/ask 规则。对标 Claude Code 的 settings.permissions。

    三张表的关系:deny 最强(永远拒),ask 次之(强制询问,压过 allow 与各模式的自动放行),
    allow 最弱(自动放行)。ask 的用处是"在一片宽 allow 里挖个洞":比如 allow 了 write_file,
    但 `ask: ["write_file(secrets/*)"]` 让写敏感目录仍然必须问人。
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


def load_permission_settings(path: Path | None = None) -> PermissionSettings:
    """加载权限配置。

    优先级:显式 path > 环境变量 WRIGHT_PERMISSION_CONFIG > ~/.wright/
    permission_settings.json > 包内默认文件。显式 path 缺失时返回空配置,
    不继续往下找。解析失败直接抛,宁可启动报错也不要静默放行一份坏配置。
    """
    if path is not None:
        if not path.is_file():
            return PermissionSettings()
        return PermissionSettings.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    for candidate in (_env_path(), _user_path(), _packaged_path()):
        if candidate is not None and candidate.is_file():
            return PermissionSettings.from_dict(
                json.loads(candidate.read_text(encoding="utf-8"))
            )
    return PermissionSettings()


def default_settings_path() -> Path:
    """"别再问"写回的路径(env 覆盖 > ~/.wright/permission_settings.json)。

    不写包内默认文件:安装后那份可能只读,也不该被一次本地确认改掉。
    注意它不含显式 path 分支——那是调用方临时指定的,不该被持久化反向写回。
    """
    return _env_path() or _user_path()


def append_allow_rule(rule: str, path: Path | None = None) -> None:
    """把一条规则追加进配置的 permissions.allow 并写回磁盘(去重)。

    这是交互式"Yes, 别再问"的持久化落点:把这次的人工放行固化成一条规则,
    下次同类调用会在【规则层】就被自动 allow,连交互 handler 都到不了。
    首次写用户文件时从包内默认配置拷一份再追加,避免丢掉预置 allow 规则。
    保持 indent=2,人能直接看/改。
    """
    def update(data: dict) -> None:
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        if rule not in allow:
            allow.append(rule)

    _update_settings(update, path)


def append_additional_directory(directory: str, path: Path | None = None) -> None:
    """Append an extra working directory to permissions.additionalDirectories."""
    resolved = str(Path(directory).expanduser().resolve())

    def update(data: dict) -> None:
        extra = data.setdefault("permissions", {}).setdefault("additionalDirectories", [])
        if resolved not in extra:
            extra.append(resolved)

    _update_settings(update, path)


def _update_settings(update: Callable[[dict], None], path: Path | None) -> None:
    """Read/merge/atomically replace settings under one process lock."""

    target = path or default_settings_path()
    with _CONFIG_LOCK:
        if target.is_file():
            data = json.loads(target.read_text(encoding="utf-8"))
        elif path is None and _packaged_path().is_file():
            data = json.loads(_packaged_path().read_text(encoding="utf-8"))
        else:
            data = {"mode": "default", "permissions": {"allow": [], "deny": []}}
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        update(data)
        if json.dumps(data, ensure_ascii=False, sort_keys=True) == before:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _env_path() -> Path | None:
    raw = os.getenv("WRIGHT_PERMISSION_CONFIG")
    return Path(raw).expanduser().resolve() if raw else None


def _user_path() -> Path:
    from ..paths import user_permission_settings_path

    return user_permission_settings_path()


def _packaged_path() -> Path:
    return Path(__file__).resolve().parent / "permission_settings.json"


def _has_shell_composition(command: str) -> bool:
    """Conservatively reject compound/dynamic shell syntax in scoped allows."""
    return bool(
        re.search(r"(?:\n|\r|&&|\|\||[;|`]|\$\(|[<>]\(|(?:^|\s)[0-9&]*>{1,2})", command)
    )
