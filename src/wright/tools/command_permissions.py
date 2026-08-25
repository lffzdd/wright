import re
import shlex
from pathlib import Path

from ..permission import PermissionCheckResult
from .base import ToolRuntime


_READ_ONLY_COMMANDS = {
    "cat",
    "cut",
    "diff",
    "du",
    "echo",
    "grep",
    "head",
    "ls",
    "pwd",
    "rg",
    "stat",
    "tail",
    "tree",
    "uname",
    "uniq",
    "wc",
    "which",
}
_READ_ONLY_GIT_SUBCOMMANDS = {
    "diff",
    "log",
    "show",
    "status",
    "rev-parse",
    "ls-files",
}


def is_execute_command_concurrency_safe(args: dict) -> bool:
    """保守识别只读 shell；解析不清、会改 cwd 或可能写入时一律排他。"""
    command = str(args.get("command", "")).strip()
    if not command or _has_output_redirection(command):
        return False
    if "$(" in command or "`" in command or "<(" in command or ">(" in command:
        return False
    if re.search(r"(^|[;&|]\s*)cd(?:\s|$)", command):
        return False

    tokens = _shell_tokens(command)
    names = _command_names(tokens)
    if not names:
        return False
    if "git" in names:
        try:
            git_index = next(
                i for i, token in enumerate(tokens) if Path(token).name == "git"
            )
            subcommand = tokens[git_index + 1]
        except (StopIteration, IndexError):
            return False
        if subcommand not in _READ_ONLY_GIT_SUBCOMMANDS:
            return False
        names.remove("git")

    return names <= _READ_ONLY_COMMANDS


def check_execute_command_permission(
    args: dict, runtime: ToolRuntime
) -> PermissionCheckResult:
    command = str(args.get("command", ""))
    risk_flags = (
        "writes_files",
        "accesses_network",
        "executes_shell",
        "may_modify_git",
        "may_delete_files",
        *_command_risk_flags(command),
    )
    return PermissionCheckResult(
        "ask",
        _format_reason(
            runtime.tool_name or "execute_command",
            "requires user approval by command tool policy",
            tuple(dict.fromkeys(risk_flags)),
        ),
        tuple(dict.fromkeys(risk_flags)),
        source="tool",
    )


def _command_risk_flags(command: str) -> tuple[str, ...]:
    flags: list[str] = []
    tokens = _shell_tokens(command)
    command_names = _command_names(tokens)

    if {"rm", "rmdir", "unlink"} & command_names:
        flags.append("deletes_files")
    if "mv" in command_names:
        flags.append("moves_files")
    if _has_output_redirection(command):
        flags.append("writes_via_redirection")
    if _has_git_state_change(tokens):
        flags.append("modifies_git_state")
    if {"curl", "wget"} & command_names:
        flags.append("network_fetch")
    if _uses_package_manager(command_names):
        flags.append("package_manager")

    return tuple(flags)


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _command_names(tokens: list[str]) -> set[str]:
    names: set[str] = set()
    separators = {"&&", "||", ";", "|", "(", ")"}
    expect_command = True

    for token in tokens:
        if token in separators:
            expect_command = True
            continue
        if "=" in token and not token.startswith(("-", "./", "/")) and expect_command:
            continue
        if expect_command:
            names.add(Path(token).name)
            expect_command = False

    return names


def _has_output_redirection(command: str) -> bool:
    return bool(re.search(r"(^|[^<>])(?:>>?|[12]>>?|&>)\s*\S+", command))


def _has_git_state_change(tokens: list[str]) -> bool:
    state_changing = {
        "add",
        "commit",
        "reset",
        "checkout",
        "clean",
        "rebase",
        "merge",
        "push",
    }

    for idx, token in enumerate(tokens[:-1]):
        if Path(token).name == "git" and tokens[idx + 1] in state_changing:
            return True
    return False


def _uses_package_manager(command_names: set[str]) -> bool:
    return bool(
        {
            "npm",
            "pnpm",
            "yarn",
            "pip",
            "pip3",
            "uv",
            "poetry",
            "cargo",
            "brew",
            "apt",
            "apt-get",
        }
        & command_names
    )


def _format_reason(
    tool_name: str,
    policy_reason: str,
    risk_flags: tuple[str, ...],
) -> str:
    suffix = f"; risks={', '.join(risk_flags)}" if risk_flags else ""
    return f"{tool_name}: {policy_reason}{suffix}"
