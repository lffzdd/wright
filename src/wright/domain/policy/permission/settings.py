"""Parsed permission rules. Persistence of the settings file is not this module."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

PermissionMode = Literal["default", "acceptEdits", "bypass", "plan"]
_VALID_MODES = ("default", "acceptEdits", "bypass", "plan")
_NETWORK_TOOLS = frozenset({"http_request", "web_fetch"})
_HTTP_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class MatchContext:
    """Canonical resources the execution backend already resolved.

    File and network rules match these identities. Raw argument strings are
    not a second, wider identity.
    """

    session_origin: str = ""
    files: tuple[tuple[str, str], ...] = ()
    url: str = ""
    method: str = ""


@dataclass(frozen=True)
class ShellAnalysis:
    simple: bool
    analyzable: bool
    segments: tuple[str, ...]


def analyze_shell(command: str) -> ShellAnalysis:
    """Split a command into simple segments, or mark it unanalyzable.

    Unanalyzable syntax (newlines, substitutions, process substitution,
    unclosed quotes) must not be treated as a simple allow. Deny and ask
    still apply to it.
    """

    if any(token in command for token in ("$(", "`", "<(", ">(", "${", "\n", "\r")):
        return ShellAnalysis(False, False, ())
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    saw_redirect = False
    i = 0
    length = len(command)

    def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            segments.append(text)

    while i < length:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or command[i - 1] != "\\"):
                quote = ""
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            flush()
            i += 2
            continue
        if ch in {";", "|", "&"}:
            flush()
            i += 1
            continue
        if ch in {"<", ">"} or (
            ch.isdigit() and _redirect_after_fd(command, i)
        ):
            if ch.isdigit():
                while buf and buf[-1].isdigit():
                    buf.pop()
                while i < length and command[i].isdigit():
                    i += 1
            flush()
            saw_redirect = True
            while i < length and command[i] in "<>&":
                i += 1
            while i < length and command[i] == " ":
                i += 1
            while i < length and command[i] not in " \t;|&<>":
                if command.startswith("&&", i) or command.startswith("||", i):
                    break
                i += 1
            continue
        buf.append(ch)
        i += 1
    if quote:
        return ShellAnalysis(False, False, ())
    flush()
    if not segments:
        return ShellAnalysis(False, False, ())
    return ShellAnalysis(len(segments) == 1 and not saw_redirect, True, tuple(segments))


def _redirect_after_fd(command: str, index: int) -> bool:
    cursor = index
    while cursor < len(command) and command[cursor].isdigit():
        cursor += 1
    return cursor < len(command) and command[cursor] in {"<", ">"}


def shell_rule_matches(pattern: str | None, command: str, *, effect: str) -> bool:
    """Allow only a simple command. Deny and ask survive compound syntax."""

    if pattern is None:
        return True
    analysis = analyze_shell(command)
    if effect == "allow":
        return bool(analysis.simple and analysis.segments and fnmatch(analysis.segments[0], pattern))
    if not analysis.analyzable:
        return True
    return any(fnmatch(segment, pattern) for segment in analysis.segments)


def parse_http_origin(url: str) -> tuple[str, str, int] | None:
    """Return scheme, host and port. Userinfo is not part of the origin."""

    parsed = urlsplit(url.strip())
    if parsed.scheme not in _HTTP_SCHEMES or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.lower().rstrip(".")
    if not host or ".." in host:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, host, port


def redact_http_target(url: str) -> str:
    """Show method target without userinfo, credentials or a raw body."""

    parsed = urlsplit(url.strip())
    origin = parse_http_origin(url)
    if origin is None:
        return "(unparsed url)"
    scheme, host, port = origin
    default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if default else f"{host}:{port}"
    return f"{scheme}://{netloc}{parsed.path or '/'}"


def relative_to_root(path: str, root: str) -> str | None:
    """Return a posix relative path, or None when path escapes root."""

    if not path or not root:
        return None
    try:
        if os.path.commonpath((path, root)) != root:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(path, root)
    if relative == "." or relative.startswith(".."):
        return None
    return PurePosixPath(relative).as_posix()


def component_match(pattern: str, relative: str) -> bool:
    """Match one path segment per pattern segment. ``*`` is one segment."""

    pattern_parts = PurePosixPath(pattern).parts
    path_parts = PurePosixPath(relative).parts
    if any(part in {"..", ""} for part in (*pattern_parts, *path_parts)):
        return False
    if "**" in pattern:
        return False

    def matches(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
        if not left:
            return not right
        head, tail = left[0], left[1:]
        if head == "*":
            return bool(right) and matches(tail, right[1:])
        if not right or not fnmatch(right[0], head):
            return False
        return matches(tail, right[1:])

    return matches(pattern_parts, path_parts)


def lexical_relative(subject: str) -> str | None:
    """Normalize a relative subject without treating it as a filesystem lookup."""

    parts: list[str] = []
    for part in PurePosixPath(subject.replace("\\", "/")).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts or PurePosixPath(subject.replace("\\", "/")).is_absolute():
        return None
    return "/".join(parts)


@dataclass(frozen=True)
class PermissionRule:
    """One permission rule.

    ``kind="tool"`` is an explicit full-tool rule from configuration. File,
    network and shell rules are bounded and are not the same thing.
    """

    tool_name: str
    subject_glob: str | None = None
    kind: Literal["tool", "file", "network", "shell"] = "tool"
    root: str = ""
    pattern: str = ""
    operations: tuple[str, ...] = ()
    scheme: str = ""
    host: str = ""
    port: int = 0
    methods: tuple[str, ...] = ()
    unresolved: bool = False
    legacy_text: str = ""

    @classmethod
    def parse(cls, raw: str, *, effect: str = "allow") -> PermissionRule:
        raw = raw.strip()
        if raw.endswith(")") and "(" in raw:
            name, _, rest = raw.partition("(")
            tool_name = name.strip()
            glob = rest[:-1].strip() or None
            if tool_name == "execute_command":
                return cls(tool_name, glob, kind="shell", pattern=glob or "")
            if tool_name in _NETWORK_TOOLS:
                # A remembered prefix such as ``https://example.com*`` is not
                # an origin. Allow does not keep that grant. Deny/ask stay
                # closed for the whole tool rather than being dropped.
                return cls(
                    tool_name,
                    glob,
                    kind="network",
                    unresolved=True,
                    legacy_text=raw,
                )
            return cls(
                tool_name,
                glob,
                kind="file",
                pattern=glob or "",
                unresolved=effect == "allow",
                legacy_text=raw,
            )
        return cls(raw, None, kind="tool")

    @classmethod
    def from_persistent(cls, item: str | dict) -> PermissionRule:
        if isinstance(item, str):
            return cls.parse(item)
        if isinstance(item, dict):
            return cls.from_mapping(item)
        raise TypeError("permission rule must be a string or a structured record")

    @classmethod
    def from_mapping(cls, data: dict) -> PermissionRule:
        kind = str(data.get("kind", "tool"))
        tool_name = str(data.get("tool_name", ""))
        if data.get("unresolved"):
            legacy = str(data.get("legacy_text", ""))
            return cls.parse(legacy or tool_name, effect="allow")
        if kind == "file":
            return cls(
                tool_name,
                None,
                kind="file",
                root=str(data.get("root", "")),
                pattern=str(data.get("pattern", "")),
                operations=tuple(str(item) for item in data.get("operations", ())),
            )
        if kind == "network":
            return cls(
                tool_name,
                None,
                kind="network",
                scheme=str(data.get("scheme", "")),
                host=str(data.get("host", "")),
                port=int(data.get("port", 0)),
                methods=tuple(str(item).upper() for item in data.get("methods", ())),
            )
        if kind == "shell":
            pattern = str(data.get("pattern", ""))
            return cls(tool_name, pattern or None, kind="shell", pattern=pattern)
        return cls(tool_name, None, kind="tool")

    @classmethod
    def file_grant(
        cls,
        tool_name: str,
        root: str,
        pattern: str,
        operations: tuple[str, ...],
    ) -> PermissionRule:
        return cls(
            tool_name,
            None,
            kind="file",
            root=root,
            pattern=pattern,
            operations=operations,
        )

    @classmethod
    def network_grant(
        cls,
        tool_name: str,
        scheme: str,
        host: str,
        port: int,
        methods: tuple[str, ...],
    ) -> PermissionRule:
        return cls(
            tool_name,
            None,
            kind="network",
            scheme=scheme,
            host=host,
            port=port,
            methods=tuple(method.upper() for method in methods),
        )

    def to_persistent(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind, "tool_name": self.tool_name}
        if self.kind == "file":
            payload.update(
                root=self.root,
                pattern=self.pattern,
                operations=list(self.operations),
            )
        elif self.kind == "network":
            payload.update(
                scheme=self.scheme,
                host=self.host,
                port=self.port,
                methods=list(self.methods),
            )
        elif self.kind == "shell":
            payload.update(pattern=self.subject_glob or self.pattern)
        if self.unresolved:
            payload["unresolved"] = True
            payload["legacy_text"] = self.legacy_text or self.tool_name
        return payload

    def describe(self) -> str:
        if self.kind == "network" and not self.unresolved:
            methods = ", ".join(self.methods) or "no methods"
            return (
                f"{self.tool_name} {methods} {self.scheme}://{self.host}:{self.port} "
                "(this origin and these methods only; userinfo is not part of the grant)"
            )
        if self.kind == "file" and self.root and not self.unresolved:
            operations = ", ".join(self.operations) or "the approved file operation"
            return (
                f"{self.tool_name} under {self.root} matching {self.pattern} "
                f"for {operations} only"
            )
        if self.kind == "shell" and self.subject_glob:
            return f"{self.tool_name} one simple command matching {self.subject_glob}"
        if self.kind == "tool":
            return f"{self.tool_name} (explicit full-tool rule)"
        return self.legacy_text or self.tool_name

    def matches(
        self,
        tool_name: str,
        subject: str,
        *,
        effect: str = "allow",
        context: MatchContext | None = None,
    ) -> bool:
        if self.tool_name != tool_name:
            return False
        if self.unresolved:
            # Ambiguous legacy allow must not grant anything. Ambiguous
            # deny/ask stays in force for this tool instead of being dropped.
            return effect != "allow"
        if self.kind == "tool":
            return True
        if self.kind == "shell":
            return shell_rule_matches(self.subject_glob, subject, effect=effect)
        if self.kind == "network":
            return _network_matches(self, subject, context)
        if self.kind == "file":
            return _file_matches(self, subject, effect, context)
        if self.subject_glob is None:
            return True
        return effect != "allow"


def _network_matches(rule: PermissionRule, subject: str, context: MatchContext | None) -> bool:
    if not rule.scheme or not rule.host or not rule.port:
        return False
    target = context.url if context is not None and context.url else subject
    origin = parse_http_origin(target)
    if origin is None:
        return False
    if origin != (rule.scheme, rule.host, rule.port):
        return False
    method = (context.method if context is not None and context.method else "GET").upper()
    if rule.methods and method not in rule.methods:
        return False
    return True


def _file_matches(
    rule: PermissionRule,
    subject: str,
    effect: str,
    context: MatchContext | None,
) -> bool:
    pattern = rule.pattern or rule.subject_glob or ""
    if not pattern or ".." in PurePosixPath(pattern).parts or "**" in pattern:
        return effect != "allow"
    if rule.root:
        if context is None:
            return False
        for path, operation in context.files:
            if rule.operations and operation not in rule.operations:
                continue
            relative = relative_to_root(path, rule.root)
            if relative and component_match(pattern, relative):
                return True
        return False
    if effect == "allow":
        return False
    if context is not None and context.files and context.session_origin:
        for path, _operation in context.files:
            if pattern.startswith("/"):
                if path == pattern:
                    return True
                continue
            relative = relative_to_root(path, context.session_origin)
            if relative and component_match(pattern, relative):
                return True
        return False
    if pattern.startswith("/"):
        return subject == pattern
    relative = lexical_relative(subject)
    if relative and component_match(pattern, relative):
        return True
    if ".." in subject or subject.startswith(("/", "~")):
        return True
    return False


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
            allow=[_load_rule(item, "allow") for item in perms.get("allow", [])],
            deny=[_load_rule(item, "deny") for item in perms.get("deny", [])],
            ask=[_load_rule(item, "ask") for item in perms.get("ask", [])],
            additional_directories=[
                str(item) for item in perms.get("additionalDirectories", [])
                if isinstance(item, str) and item.strip()
            ],
        )


def _load_rule(item: object, effect: str) -> PermissionRule:
    if isinstance(item, str):
        return PermissionRule.parse(item, effect=effect)
    if isinstance(item, dict):
        return PermissionRule.from_mapping(item)
    raise ValueError("permission rule must be a string or an object")
