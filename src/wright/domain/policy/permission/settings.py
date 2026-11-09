"""Parsed permission rules. Persistence of the settings file is not this module."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field, fields
from fnmatch import fnmatch
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import urlsplit

from .scope import path_module

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
    cwd: str = ""
    sandbox_key: str = ""
    operations: tuple[str, ...] = ()
    executable: str = ""
    executable_target: str = ""
    execution_mode: str = ""


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
            # Only double quotes have escapes: \\ escapes itself, so \\" still closes.
            if quote == '"' and ch == "\\" and i + 1 < length:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < length:
            buf.extend((ch, command[i + 1]))
            i += 2
            continue
        if ch == "#" and (not buf or buf[-1].isspace()):
            break
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
    if any(fnmatch(segment, pattern) for segment in analysis.segments):
        return True
    programs = _deny_candidates(command)
    return programs is None or any(fnmatch(text, pattern) for text in programs)


_CONTROL_WORDS = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac", "{", "}", "!", "time"})
_PREFIX_COMMANDS = frozenset({"env", "command", "builtin", "exec", "nohup", "nice", "ionice", "time", "timeout", "sudo", "doas", "su", "xargs", "stdbuf", "setsid", "caffeinate"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_GIT_VALUE_OPTIONS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env", "--super-prefix"})


def _program_name(word: str) -> str:
    name = PurePosixPath(word).name
    return name[:-4] if name.lower().endswith(".exe") else name


def _shell_words(command: str) -> tuple[tuple[tuple[str, bool], ...], ...] | None:
    """Split as bash does into commands of (word, changes at runtime); None if unclear."""
    commands: list[tuple[tuple[str, bool], ...]] = []
    words: list[tuple[str, bool]] = []
    word: list[str] = []
    started = dynamic = skip_target = False
    quote = ""
    index, length = 0, len(command)

    def end_word() -> None:
        nonlocal started, dynamic, skip_target
        if started:
            if not skip_target:
                words.append(("".join(word), dynamic))
            skip_target = False
        word.clear()
        started = dynamic = False

    def end_command() -> None:
        end_word()
        if words:
            commands.append(tuple(words))
            words.clear()

    while index < length:
        char = command[index]
        following = command[index + 1] if index + 1 < length else ""
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                word.append(char)
        elif char == "\\":
            if not following:
                return None
            if following != "\n" and (not quote or following in '"\\$`'):
                word.append(following)
            elif quote:
                word.append(char + following)
            started = True
            index += 2
            continue
        elif char == "`" or (char == "$" and following in "({"):
            return None
        elif char == "$":
            dynamic = started = True
            word.append(char)
        elif quote == '"':
            if char == '"':
                quote = ""
            else:
                word.append(char)
        elif char in "\"'":
            quote = char
            started = True
        elif char.isspace() and char != "\n":
            end_word()
        elif char == "#" and not started:
            while index < length and command[index] != "\n":
                index += 1
            continue
        elif char in "<>" or (char == "&" and following == ">"):
            if following == "(" :
                return None
            if word and "".join(word).isdigit():
                word.clear()
                started = False
            end_word()
            index += 1
            while index < length and command[index] in "<>&|":
                index += 1
            skip_target = True
            continue
        elif char in ";&|()\n":
            end_command()
        else:
            if char in "*?[" or (char == "{" and not started and following not in " \t"):
                dynamic = True
            word.append(char)
            started = True
        index += 1
    if quote:
        return None
    end_command()
    return tuple(commands)


def _deny_candidates(command: str, depth: int = 0) -> tuple[str, ...] | None:
    """Every spelling a deny rule should see; None when the program cannot be known.

    Names programs through paths, quoting, wrappers and ``sh -c``. Code a program
    runs for itself (``python -c``) stays outside what command text can show.
    """
    parsed = _shell_words(command)
    if parsed is None or depth > 3:
        return None
    result: list[str] = []
    for entry in parsed:
        index = 0
        while index < len(entry) and (
            entry[index][0] in _CONTROL_WORDS
            or (not entry[index][1] and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", entry[index][0]))
        ):
            index += 1
        if index == len(entry):
            continue
        if entry[index][1]:
            return None
        nested = _command_spellings(tuple(item[0] for item in entry[index:]), depth)
        if nested is None:
            return None
        result.extend(nested)
    return tuple(result)


def _command_spellings(argv: tuple[str, ...], depth: int) -> tuple[str, ...] | None:
    name = _program_name(argv[0])
    arguments = argv[1:]
    if name == "git":
        rest = list(arguments)
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in _GIT_VALUE_OPTIONS else rest[1:]
        arguments = tuple(rest)
    joined = " ".join((name, *arguments))
    # The trailing space lets `rm *` see a bare `rm`, whose arguments may come from a pipe.
    result = [" ".join(argv), joined, joined + " "]
    if name in _PREFIX_COMMANDS:
        for start in range(1, len(argv)):
            word = argv[start]
            if not word.startswith("-") and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
                nested = _command_spellings(argv[start:], depth)
                if nested is None:
                    return None
                result.extend(nested)
    if name == "eval":
        nested = _deny_candidates(" ".join(arguments), depth + 1)
        if nested is None:
            return None
        result.extend(nested)
    if name in _SHELLS:
        for position, word in enumerate(arguments):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word):
                if position + 1 >= len(arguments):
                    return None
                nested = _deny_candidates(arguments[position + 1], depth + 1)
                if nested is None:
                    return None
                result.extend(nested)
                break
    return tuple(result)


def direct_command_argv(command: str, *, dialect: str = "posix") -> tuple[str, ...] | None:
    """Decode one literal POSIX command, never interpret shell syntax as argv.

    Quoted script arguments may contain shell punctuation. Expansions, shell
    control syntax and wrappers require the exact whole-command approval path.
    """
    if dialect != "posix" or "\x00" in command:
        return None
    quote = ""
    escaped = False
    for index, char in enumerate(command):
        if char == "\\" and quote != "'" and index + 1 < len(command) and (command[index + 1] in "\n\r" or (quote == '"' and command[index + 1] in "$`")):
            return None
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote == "'":
            if char == "'":
                quote = ""
            continue
        if quote == '"':
            if char == '"':
                quote = ""
            elif char in "$`":
                return None
            continue
        if char in "\"'":
            quote = char
        elif char in ";|&<>()$`\n\r*?[]{}~#":
            return None
    if quote or escaped:
        return None
    try:
        argv = tuple(shlex.split(command, posix=True))
    except ValueError:
        return None
    if not argv or not argv[0] or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        return None
    # These words alter command resolution or execution context, rather than
    # directly naming the program the user is being offered authority for.
    if PurePosixPath(argv[0]).name in _SHELL_WRAPPERS or argv[0].startswith("-"):
        return None
    return argv


_SHELL_WRAPPERS = frozenset({"env", "command", "exec", "sudo", "su", "doas", "nohup", "nice", "time", "timeout", "xargs", "eval", "source", ".", "cd", "export", "unset", "alias", "unalias", "set", "builtin", "function", "if", "then", "for", "while", "until", "case", "!"})
# Harmless glue in a compound command: covered once the real programs are.
_NEUTRAL_COMMANDS = frozenset({"cd", "echo", "pwd", "true", "false", ":", "test", "["})
_DISCARD_REDIRECT = re.compile(r"(?:\d|&)?>>?\s*/dev/null(?=$|[\s;&|])|\d>&\d(?=$|[\s;&|])")


def command_segments(command: str, *, dialect: str = "posix") -> tuple[tuple[str, ...], ...] | None:
    """Split a shell line into the programs it runs, or None when that is unclear.

    Only sequencing and pipes are understood. Substitution, subshells, other
    redirects, background jobs and wrappers stay whole-command approvals.
    Neutral commands are dropped from the result.
    """
    if dialect != "posix" or "\x00" in command:
        return None
    pieces: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        following = command[index + 1] if index + 1 < len(command) else ""
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\":
            if following in ("\n", "\r", ""):
                return None
            current.append(char + following)
            index += 2
            continue
        elif char == "`" or (char == "$" and following == "("):
            return None
        elif quote == '"':
            quote = "" if char == '"' else quote
        elif char in "\"'":
            quote = char
        else:
            discard = _DISCARD_REDIRECT.match(command, index)
            if discard:
                index = discard.end()
                current.append(" ")
                continue
            if char in ";|&\n\r":
                if char == "&" and following != "&":
                    return None
                index += 2 if char in "&|" and following == char else 1
                pieces.append("".join(current))
                current = []
                continue
            if char in "<>()#":
                return None
        current.append(char)
        index += 1
    if quote:
        return None
    pieces.append("".join(current))
    segments: list[tuple[str, ...]] = []
    changes_directory = False
    for piece in pieces:
        if not piece.strip():
            if piece is pieces[-1]:
                continue
            return None
        try:
            argv = tuple(shlex.split(piece, posix=True))
        except ValueError:
            return None
        program = argv[0]
        if re.search(r"[$*?\[\]{}~=]", program) and program != "[":
            return None
        if PurePosixPath(program).name == "cd":
            changes_directory = True
        if program in _NEUTRAL_COMMANDS:
            continue
        if PurePosixPath(program).name in _SHELL_WRAPPERS or program.startswith("-"):
            return None
        # A relative path means something else after cd than when it was resolved.
        if changes_directory and "/" in program and not program.startswith("/"):
            return None
        segments.append(argv)
    return tuple(segments)


def command_dialect(cwd: str) -> str:
    # Production POSIX launchers use bash; Windows launches PowerShell and must
    # never match a rule parsed with POSIX quoting semantics.
    return "powershell" if PureWindowsPath(cwd).drive or "\\" in cwd else "posix"


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
        module = path_module(root)
        path, root = module.normpath(path), module.normpath(root)
        if module.normcase(module.commonpath((path, root))) != module.normcase(root):
            return None
    except ValueError:
        return None
    relative = module.relpath(path, root)
    if relative == "." or relative.startswith(".."):
        return None
    return PurePosixPath(relative.replace("\\", "/")).as_posix()


def component_match(pattern: str, relative: str) -> bool:
    """Match one path segment per pattern segment.

    ``*`` is exactly one segment; a whole ``**`` segment is zero or more. Any
    other use of ``**`` inside a segment is not a pattern and never matches.
    """

    pattern_parts = PurePosixPath(pattern).parts
    path_parts = PurePosixPath(relative).parts
    if any(part in {"..", ""} for part in (*pattern_parts, *path_parts)):
        return False
    if any("**" in part and part != "**" for part in pattern_parts):
        return False

    def matches(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
        if not left:
            return not right
        head, tail = left[0], left[1:]
        if head == "**":
            return any(matches(tail, right[index:]) for index in range(len(right) + 1))
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
    kind: Literal["tool", "file", "directory", "network", "shell", "command", "sandbox_network"] = "tool"
    root: str = ""
    pattern: str = ""
    operations: tuple[str, ...] = ()
    scheme: str = ""
    host: str = ""
    port: int = 0
    methods: tuple[str, ...] = ()
    unresolved: bool = False
    legacy_text: str = ""
    id: str = ""
    lifetime: Literal["session", "project", "user"] = "session"
    project_id: str = ""
    source: str = "approval"
    root_kind: Literal["absolute", "workspace"] = "absolute"
    recursive: bool = False
    exact: bool = False
    cwd: str = ""
    sandbox_key: str = ""
    execution_mode: str = "sandbox"
    argv: tuple[str, ...] = ()
    command_match: str = "executable"
    dialect: str = "posix"
    executable_target: str = ""
    path: str = ""
    path_match: str = "origin"

    def validate_value(self) -> None:
        # Rule values stay typed here; raw record validation belongs to codecs.
        for descriptor in fields(self):
            value = getattr(self, descriptor.name)
            if not isinstance(value, (str, int, bool, tuple, type(None))):
                raise TypeError("Session rules must contain immutable values")
            if isinstance(value, tuple) and any(
                not isinstance(item, str) for item in value
            ):
                raise TypeError("Session rule collections must contain strings")
    def validate_grant(self) -> None:
        self.validate_value()
        if (
            self.unresolved
            or not self.tool_name
            or self.kind not in {"tool", "file", "directory", "network", "shell", "command", "sandbox_network"}
        ):
            raise ValueError("Session rules require a known, resolved resource")
        if self.execution_mode not in {"sandbox", "host"}:
            raise ValueError("execution_mode must be sandbox or host")
        if self.kind == "tool":
            if self.operations and not set(self.operations) <= {
                "file_read",
                "file_write",
                "shell",
                "network_read",
                "network_write",
            }:
                raise ValueError("Tool rules require valid operations")
        if self.kind == "command":
            if (self.tool_name != "execute_command" or self.command_match not in {"executable", "argv_prefix"}
                or self.dialect != "posix" or not self.argv or not self.argv[0] or any(not isinstance(arg, str) for arg in self.argv)
                or not PurePosixPath(self.argv[0]).is_absolute() or not PurePosixPath(self.executable_target).is_absolute() or not self.cwd or not self.sandbox_key
                or (self.command_match == "executable" and len(self.argv) != 1)
                or (self.command_match == "argv_prefix" and len(self.argv) < 2)
                or self.operations != ("shell",)) :
                raise ValueError("Command grants require literal argv, cwd and sandbox capabilities")
        if self.kind == "sandbox_network":
            if (
                self.tool_name != "execute_command"
                or self.execution_mode != "sandbox"
                or self.operations != ("network_read", "network_write")
            ):
                raise ValueError("Sandbox network grants apply to sandboxed execute_command only")
        if self.kind in {"file", "directory"}:
            if not self.operations or not set(self.operations) <= {
                "file_read",
                "file_write",
            }:
                raise ValueError("File rules require file operations")
            if self.root_kind == "absolute":
                if not (
                    PurePosixPath(self.root).is_absolute()
                    or PureWindowsPath(self.root).is_absolute()
                ):
                    raise ValueError("Session resource roots must be absolute")
            elif self.root_kind != "workspace" or self.root:
                raise ValueError("Workspace rules cannot contain an absolute root")
            pattern = self.pattern.replace("\\", "/")
            if (
                ".." in PurePosixPath(pattern).parts
                or PureWindowsPath(pattern).drive
                or PurePosixPath(pattern).is_absolute()
            ):
                raise ValueError("Resource pattern must stay within its root")
            if self.kind == "file" and not pattern:
                raise ValueError("File rules require a resource pattern")

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
                if glob in {"http://*", "https://*"} and effect != "allow":
                    return cls(tool_name, kind="network", scheme=glob.split(":")[0])
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
        kind = data.get("kind")
        tool_name = data.get("tool_name", "")
        if not isinstance(tool_name, str):
            raise TypeError("tool_name must be a string")
        for key in ("recursive", "exact", "unresolved"):
            if key in data and not isinstance(data[key], bool):
                raise ValueError(f"{key} must be a boolean")
        if kind not in {"tool", "file", "directory", "network", "shell", "command", "sandbox_network"} or not tool_name:
            raise ValueError("a permission rule requires a known kind and a tool_name")
        lifetime = data.get("lifetime", "session")
        root_kind = data.get("root_kind", "absolute")
        if lifetime not in {"session", "project", "user"} or root_kind not in {"absolute", "workspace"}:
            raise ValueError("invalid permission lifetime or root kind")
        operations = data.get("operations", [])
        methods = data.get("methods", [])
        argv = data.get("argv", [])
        if not isinstance(argv, (list, tuple)) or any(not isinstance(arg, str) for arg in argv):
            raise TypeError("argv must be an array of strings")
        if not isinstance(operations, (list, tuple)) or not isinstance(methods, (list, tuple)):
            raise TypeError("operations and methods must be arrays")
        if any(op not in {"file_read", "file_write", "shell", "network_read", "network_write"} for op in operations):
            raise ValueError("invalid permission operation")
        if kind in {"file", "directory"}:
            root = data.get("root", "")
            pattern = data.get("pattern", "")
            if not isinstance(root, str) or not isinstance(pattern, str) or not operations:
                raise ValueError("file permissions require a root, pattern and operations")
            if root_kind == "absolute" and not (PurePosixPath(root).is_absolute() or PureWindowsPath(root).is_absolute()):
                raise ValueError("permission root must be absolute")
            if root_kind == "workspace" and root:
                raise ValueError("Workspace rules cannot contain an absolute root")
            if ".." in PurePosixPath(pattern.replace("\\", "/")).parts or PureWindowsPath(pattern).drive or PurePosixPath(pattern).is_absolute():
                raise ValueError("Resource pattern must stay within its root")
            if kind == "file" and not pattern:
                raise ValueError("file pattern must stay within its root")
        for key in ("id", "project_id", "source", "cwd", "sandbox_key", "execution_mode", "scheme", "host", "pattern", "command_match", "dialect", "executable_target", "path", "path_match"):
            if key in data and not isinstance(data[key], str):
                raise TypeError(f"{key} must be a string")
        if any(not isinstance(method, str) or not method.isalpha() for method in methods):
            raise ValueError("HTTP methods must be nonempty names")
        if kind == "network":
            scheme, host, port = data.get("scheme", ""), data.get("host", ""), data.get("port", 0)
            if scheme not in _HTTP_SCHEMES or not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
                raise ValueError("Invalid network scheme or port")
            if host and (not port or any(char in host for char in "/@* ") or not methods):
                raise ValueError("Network authorizations require an exact host, port and methods")
        path_match = data.get("path_match", "origin")
        path = data.get("path", "")
        if kind == "network" and (path_match not in {"origin", "exact"} or not isinstance(path, str) or (path_match == "exact" and (not path.startswith("/") or "?" in path or "#" in path))):
            raise ValueError("Network paths require exact absolute URL paths or origin scope")
        common = {"source": data.get("source", "approval"), "id": data.get("id", ""), "lifetime": lifetime,
                  "project_id": data.get("project_id", ""), "root_kind": root_kind, "recursive": data.get("recursive", False),
                  "exact": data.get("exact", False), "cwd": data.get("cwd", ""), "sandbox_key": data.get("sandbox_key", ""), "execution_mode": data.get("execution_mode", "sandbox")}

        if data.get("unresolved"):
            raise ValueError("Ambiguous permission records are unsupported; approve access again")
        if kind in {"file", "directory"}:
            return cls(
                tool_name,
                None,
                kind=kind,
                root=str(data.get("root", "")),
                pattern=str(data.get("pattern", "")),
                operations=tuple(str(item) for item in data.get("operations", ())),
                **common,
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
                path=path, path_match=path_match,
                **common,
            )
        if kind == "command":
            rule = cls(tool_name, kind="command", argv=tuple(argv), command_match=data.get("command_match", "executable"), dialect=data.get("dialect", "posix"), executable_target=data.get("executable_target", ""), operations=tuple(operations), **common)
            rule.validate_grant()
            return rule
        if kind == "sandbox_network":
            rule = cls(
                tool_name,
                kind="sandbox_network",
                operations=tuple(str(item) for item in operations) or ("network_read", "network_write"),
                **common,
            )
            rule.validate_grant()
            return rule
        if kind == "shell":
            pattern = str(data.get("pattern", ""))
            if common["exact"] and (not pattern or not common["cwd"]):
                raise ValueError("remembered shell commands require an exact command and cwd")
            return cls(tool_name, pattern or None, kind="shell", pattern=pattern, **common)
        rule = cls(tool_name, None, kind="tool", operations=tuple(operations), **common)
        rule.validate_grant()
        return rule

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

    @classmethod
    def sandbox_network_grant(cls) -> PermissionRule:
        return cls(
            "execute_command",
            kind="sandbox_network",
            operations=("network_read", "network_write"),
            execution_mode="sandbox",
        )

    def to_persistent(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind, "tool_name": self.tool_name}
        if self.kind in {"file", "directory"}:
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
                path=self.path, path_match=self.path_match,
            )
        elif self.kind == "command":
            payload.update(argv=list(self.argv), command_match=self.command_match, dialect=self.dialect, executable_target=self.executable_target, operations=list(self.operations))
        elif self.kind == "sandbox_network":
            payload.update(operations=list(self.operations))
        elif self.kind == "tool" and self.operations:
            payload.update(operations=list(self.operations))
        elif self.kind == "shell":
            payload.update(pattern=self.subject_glob or self.pattern)
        if self.unresolved:
            payload["unresolved"] = True
            payload["legacy_text"] = self.legacy_text or self.tool_name
        payload.update(id=self.id, lifetime=self.lifetime, project_id=self.project_id, source=self.source, root_kind=self.root_kind,
                       recursive=self.recursive, exact=self.exact, cwd=self.cwd, sandbox_key=self.sandbox_key)
        if self.kind in {"shell", "command", "sandbox_network"}:
            payload["execution_mode"] = self.execution_mode
        return payload

    def resource_root(self, workspace: str) -> str:
        root = workspace if self.root_kind == "workspace" else self.root
        return path_module(root).normpath(path_module(root).join(root, self.pattern)) if self.kind == "directory" and self.pattern else root

    def display_target(self, workspace: str) -> str:
        if self.kind == "file":
            root = self.resource_root(workspace)
            return path_module(root).normpath(path_module(root).join(root, self.pattern))
        if self.kind == "directory":
            return self.resource_root(workspace)
        if self.kind == "network":
            return (f"{self.scheme}://{self.host}:{self.port}" + (self.path if self.path_match == "exact" else "")) if self.host else f"{self.scheme}://*"
        if self.kind == "command":
            return shlex.join(self.argv)
        if self.kind == "sandbox_network":
            return "sandbox network"
        if self.kind == "shell":
            return self.pattern
        return self.tool_name

    def describe(self) -> str:
        if self.kind == "command":
            scope = "any arguments" if self.command_match == "executable" else "argv prefix"
            boundary = "on the host without OS isolation" if self.execution_mode == "host" else "within the sandbox"
            return f"{shlex.join(self.argv)} ({scope}; includes scripts and subprocesses {boundary})"
        if self.kind == "network" and not self.unresolved:
            methods = ", ".join(self.methods) or "no methods"
            return (
                f"{self.tool_name} {methods} {self.scheme}://{self.host}:{self.port}{self.path if self.path_match == 'exact' else ''} "
                f"({self.path_match} scope and these methods only; userinfo is not part of the grant)"
            )
        if self.kind in {"file", "directory"} and not self.unresolved:
            operations = ", ".join(self.operations) or "the approved file operation"
            return (
                f"{self.tool_name} under {self.root or 'current workspace'} matching {self.pattern or '(directory tree)'} "
                f"for {operations} only"
            )
        if self.kind == "shell" and self.subject_glob:
            return f"{self.tool_name} one simple command matching {self.subject_glob}"
        if self.kind == "sandbox_network":
            return "sandbox commands may use the network; file isolation remains"
        if self.kind == "tool":
            if self.operations:
                return f"{self.tool_name} ({', '.join(self.operations)} only)"
            return f"{self.tool_name} (explicit full-tool rule)"
        return self.legacy_text or self.tool_name

    def covers(self, executable: str, executable_target: str, arguments: tuple[str, ...], execution_mode: str) -> bool:
        """Program identity only: Shell cwd stays in scope and the sandbox still bounds it."""
        return (self.kind == "command" and execution_mode == self.execution_mode
                and executable_target == self.executable_target
                and (executable, *arguments)[:len(self.argv)] == self.argv)

    def matches(
        self,
        tool_name: str,
        subject: str,
        *,
        effect: str = "allow",
        context: MatchContext | None = None,
    ) -> bool:
        if self.tool_name != tool_name and not (self.kind in {"file", "directory"} and self.tool_name == "*"):
            return False
        if self.unresolved:
            # Ambiguous legacy allow must not grant anything. Ambiguous
            # deny/ask stays in force for this tool instead of being dropped.
            return effect != "allow"
        if self.kind == "sandbox_network":
            # Capability only: evaluate() checks this separately so a network
            # grant cannot become a full-tool allow for every command.
            return False
        if self.kind == "tool":
            return not self.operations or (context is not None and set(context.operations) <= set(self.operations))
        if self.kind == "command":
            if context is None or command_dialect(context.cwd) != self.dialect:
                return False
            argv = direct_command_argv(subject, dialect=self.dialect)
            return argv is not None and self.covers(context.executable, context.executable_target, argv[1:], context.execution_mode)
        if self.kind == "shell":
            if self.exact:
                return subject == self.pattern and context is not None and context.cwd == self.cwd and (not self.sandbox_key or context.sandbox_key == self.sandbox_key)
            return shell_rule_matches(self.subject_glob, subject, effect=effect)
        if self.kind == "network":
            if effect != "allow" and self.scheme and not self.host:
                origin = parse_http_origin(context.url if context else subject)
                return origin is not None and origin[0] == self.scheme
            return _network_matches(self, subject, context)
        if self.kind in {"file", "directory"}:
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
    if rule.path_match == "exact" and (urlsplit(target).path or "/") != rule.path:
        return False
    if rule.methods and method not in rule.methods:
        return False
    return True


def _file_matches(
    rule: PermissionRule,
    subject: str,
    effect: str,
    context: MatchContext | None,
) -> bool:
    if rule.kind == "directory":
        if context is None or not context.files:
            return False
        root = rule.resource_root(context.session_origin)
        matches = [
            (path_module(root).normcase(path) == path_module(root).normcase(root) or (relative_to_root(path, root) is not None and (rule.recursive or path_module(root).dirname(path) == root)))
            and (not rule.operations or operation in rule.operations)
            for path, operation in context.files
        ]
        return all(matches) if effect == "allow" else any(matches)
    pattern = rule.pattern or rule.subject_glob or ""
    if not pattern or ".." in PurePosixPath(pattern).parts:
        return effect != "allow"
    if "**" in pattern and (effect == "allow" or any("**" in part and part != "**" for part in PurePosixPath(pattern).parts)):
        # Allow stays bounded; a malformed recursive deny/ask stays closed.
        return effect != "allow"
    root = context.session_origin if context and rule.root_kind == "workspace" else rule.root
    if root:
        if context is None:
            return False
        matches = []
        for path, operation in context.files:
            relative = relative_to_root(path, root)
            matches.append(bool((not rule.operations or operation in rule.operations) and relative and (
                path_module(root).normcase(relative) == path_module(root).normcase(pattern) if rule.exact else component_match(pattern, relative)
            )))
        return bool(matches) and (all(matches) if effect == "allow" else any(matches))
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
    project_rules: list[PermissionRule] = field(default_factory=list)
    managed: bool = False
    revision: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> PermissionSettings:
        if not isinstance(data, dict) or data.get("version") != 2:
            raise ValueError("permissions require version 2")
        mode = data.get("mode", "default")
        if mode not in _VALID_MODES:
            raise ValueError(
                f"Unknown permission mode {mode!r}; choices: {', '.join(_VALID_MODES)}"
            )
        perms = data.get("permissions", {})
        if not isinstance(perms, dict):
            raise TypeError("permissions must be an object")
        if "additionalDirectories" in perms:
            raise ValueError("Use bounded directory authorization records instead of additionalDirectories")
        if not isinstance(perms, dict) or any(not isinstance(perms.get(key, []), list) for key in ("allow", "deny", "ask")):
            raise ValueError("permission allow/deny/ask must be arrays")
        return cls(
            mode=mode,
            revision=str(data.get("revision", "")),
            allow=[_load_rule(item, "allow") for item in perms.get("allow", [])],
            deny=[_load_rule(item, "deny") for item in perms.get("deny", [])],
            ask=[_load_rule(item, "ask") for item in perms.get("ask", [])],

        )


def _load_rule(item: object, effect: str) -> PermissionRule:
    if isinstance(item, str):
        rule = PermissionRule.parse(item, effect=effect)
    elif isinstance(item, dict):
        rule = PermissionRule.from_mapping(item)
    else:
        raise TypeError("permission rule must be a string or an object")
    if rule.unresolved:
        raise ValueError("Ambiguous permission syntax is unsupported; use a bounded resource record")
    if effect == "allow" and rule.kind in {"file", "directory"} and "**" in rule.pattern:
        raise ValueError("`**` is only supported in deny and ask rules; grant a directory instead")
    if effect == "allow" and rule.tool_name == "execute_command":
        if rule.kind == "command":
            pass
        elif rule.kind == "shell":
            if not rule.exact or rule.lifetime != "session" or not rule.sandbox_key:
                raise ValueError(
                    "Shell grants require an exact session command, cwd and sandbox capability"
                )
        elif rule.kind == "sandbox_network":
            rule.validate_grant()
        else:
            raise ValueError(
                "execute_command allow rules must be command grants, exact session shell grants, or sandbox network grants"
            )
    return rule


@dataclass(frozen=True)
class PermissionConfiguration:
    """Immutable policy input; mutable settings exist only at the decoding boundary."""
    mode: PermissionMode
    allow: tuple[PermissionRule, ...]
    deny: tuple[PermissionRule, ...]
    ask: tuple[PermissionRule, ...]
    project_rules: tuple[PermissionRule, ...]
    revision: str

    def __post_init__(self) -> None:
        for name in ("allow", "deny", "ask", "project_rules"):
            rules = tuple(getattr(self, name))
            for rule in rules:
                if not isinstance(rule, PermissionRule):
                    raise TypeError("Policy input requires permission rule values")
                rule.validate_value()
            object.__setattr__(self, name, rules)

    @classmethod
    def from_settings(cls, settings: PermissionSettings) -> PermissionConfiguration:
        return cls(settings.mode, tuple(settings.allow), tuple(settings.deny), tuple(settings.ask), tuple(settings.project_rules), settings.revision)
