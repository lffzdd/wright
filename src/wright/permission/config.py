"""规则式权限裁决:把 ask 交给一份「权限模式 + allow/deny 规则」的配置来自动判定。

为什么要这一层
--------------
Tool.check_permission 只会把有副作用的工具(写文件/改文件/跑命令/发网络)标成
`ask`——它不知道"这次该不该放行",那是策略问题。`PermissionResolver` 在拿到 `ask`
后会回调一个 `approval_handler` 来裁决;本模块就是这个 handler 的【非交互】实现:
不弹终端、不等人回车,而是按一份持久化配置(权限模式 + 规则)自动给出 allow/deny。
这正好补上"main.py 全自动跑、没人逐个确认导致 ask 一律 fail-closed"的缺口。

判定顺序(fail-closed:拿不准就拒)
----------------------------------
1. 系统边界:带 `cwd_outside_workspace` 风险的一律拒(越出 workspace 不容商量)。
2. deny 规则命中 → 拒(deny 永远压过 allow)。
3. 权限模式特判:
   - bypass     → 放行(只受 deny 与系统边界约束),给可信无人值守。
   - plan       → 拒一切有副作用的调用(能走到这里的本就都带副作用)。
   - acceptEdits→ 风险只涉及读写本地文件(无 shell/网络)时自动放行。
4. allow 规则命中 → 放行。
5. 都没命中 → 拒,并在 reason 里说明"没有匹配的 allow 规则"。

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
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

from .resolver import PermissionRequest
from .types import PermissionCheckResult


PermissionMode = Literal["default", "acceptEdits", "bypass", "plan"]
_VALID_MODES = ("default", "acceptEdits", "bypass", "plan")

# acceptEdits 只为"本地文件读写"开绿灯:风险标志全落在这个集合内才算"纯编辑"。
# 一旦掺入 shell/网络/git 等更重的副作用,就不在 acceptEdits 的自动放行范围。
_EDIT_ONLY_FLAGS = frozenset({"reads_files", "writes_files"})

# 走到 handler 的调用本就都带副作用;这个标志表示"系统边界被破坏",任何模式都不放行。
_HARD_DENY_FLAG = "cwd_outside_workspace"


@dataclass(frozen=True)
class PermissionRule:
    """一条解析后的规则:工具名 +(可选)主体 glob。无 glob 即匹配该工具任意调用。"""

    tool_name: str
    subject_glob: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "PermissionRule":
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

    @classmethod
    def from_dict(cls, data: dict) -> "PermissionSettings":
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
    target = path or default_settings_path()
    if target.is_file():
        data = json.loads(target.read_text(encoding="utf-8"))
    elif path is None and _packaged_path().is_file():
        data = json.loads(_packaged_path().read_text(encoding="utf-8"))
    else:
        data = {"mode": "default", "permissions": {"allow": [], "deny": []}}

    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if rule not in allow:
        allow.append(rule)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _env_path() -> Path | None:
    raw = os.getenv("WRIGHT_PERMISSION_CONFIG")
    return Path(raw).expanduser().resolve() if raw else None


def _user_path() -> Path:
    from ..paths import user_permission_settings_path

    return user_permission_settings_path()


def _packaged_path() -> Path:
    return Path(__file__).resolve().parent / "permission_settings.json"


class RuleBasedApprovalHandler:
    """按 PermissionSettings 自动裁决 ask 的非交互 handler。

    设计成可调用对象,直接传给 PermissionResolver(approval_handler=...) 即可。
    线程安全:只读 settings、不持有可变状态,可被并发/串行批共用。
    """

    def __init__(
        self,
        settings: PermissionSettings,
        on_no_match: Literal["deny", "ask"] = "deny",
    ):
        self.settings = settings
        # 没有任何规则命中时怎么收口:
        # - "deny"(默认):独立使用时 fail-closed,无人值守跑就该拒。
        # - "ask" :作为组合链的一环时"弃权",把这次决定让给后面的 handler(如交互式)。
        #   规则只对"明确该 allow / 明确该 deny"表态,灰色地带交给人。
        self.on_no_match = on_no_match

    def __call__(self, request: PermissionRequest) -> PermissionCheckResult:
        tool_name = request.tool.name
        subject = _subject_of(tool_name, request.arguments)
        flags = request.check.risk_flags

        # 1. 系统边界:越出 workspace,任何模式都不放行。
        if _HARD_DENY_FLAG in flags:
            return self._deny(request, "越出 workspace 边界,拒绝执行")

        # 2. deny 规则压过一切。
        if self._any_match(self.settings.deny, tool_name, subject):
            return self._deny(request, f"命中 deny 规则: {tool_name}({subject})")

        # 3. ask 规则:强制询问,压过 allow 与各模式的自动放行(deny 之外谁也盖不住它)。
        #    返回 ask——独立用时 resolver 会 fail-closed(没人可问就拒),组合链里则落到
        #    后面的交互式 handler 真去问人。这就是"在宽 allow 里挖洞"的实现。
        if self._any_match(self.settings.ask, tool_name, subject):
            return PermissionCheckResult(
                "ask",
                f"命中 ask 规则,需确认: {tool_name}({subject})",
                request.check.risk_flags,
                source="rule_config",
            )

        mode = self.settings.mode

        # 4. 模式特判。
        if mode == "bypass":
            return self._allow(request, "bypass 模式放行")
        if mode == "plan":
            return self._deny(request, "plan 模式:计划阶段不执行任何副作用操作")
        if mode == "acceptEdits" and flags and set(flags) <= _EDIT_ONLY_FLAGS:
            return self._allow(request, "acceptEdits 模式:本地文件编辑自动放行")

        # 5. allow 规则命中。
        if self._any_match(self.settings.allow, tool_name, subject):
            return self._allow(request, f"命中 allow 规则: {tool_name}({subject})")

        # 6. 没有任何规则命中:按 on_no_match 收口——独立用就 fail-closed 拒,
        #    组合用就返回 ask"弃权",让链上后一个 handler(如交互式)接手。
        reason = f"无匹配的 allow 规则({mode} 模式): {tool_name}({subject})"
        if self.on_no_match == "ask":
            return PermissionCheckResult(
                "ask", reason, request.check.risk_flags, source="rule_config"
            )
        return self._deny(request, reason + ",默认拒绝")

    @staticmethod
    def _any_match(
        rules: list[PermissionRule], tool_name: str, subject: str
    ) -> bool:
        return any(rule.matches(tool_name, subject) for rule in rules)

    @staticmethod
    def _allow(request: PermissionRequest, reason: str) -> PermissionCheckResult:
        return PermissionCheckResult(
            "allow", reason, request.check.risk_flags, source="rule_config"
        )

    @staticmethod
    def _deny(request: PermissionRequest, reason: str) -> PermissionCheckResult:
        return PermissionCheckResult(
            "deny", reason, request.check.risk_flags, source="rule_config"
        )


def _subject_of(tool_name: str, arguments: dict) -> str:
    """取一个工具最有判别力的参数作为规则匹配主体。"""
    for key in ("command", "file", "directory", "url"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
