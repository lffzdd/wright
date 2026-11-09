"""Shared immutable sandbox profile interpretation; no platform dispatch."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ...domain.policy.permission.types import InvocationGrant


class SandboxUnavailable(RuntimeError):
    pass


def clean_environment() -> dict[str, str]:
    keys = {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "TZ",
            "TERM", "PYTHONIOENCODING", "VIRTUAL_ENV"}
    return {**{key: value for key, value in os.environ.items() if key in keys},
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def runtime_roots() -> tuple[Path, ...]:
    candidates = [Path(path) for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/System", "/Library/Frameworks", "/Library/Developer")]
    candidates.extend((Path(sys.base_prefix), Path(sys.executable).resolve().parent))
    for name in ("git", "node", "python3"):
        executable = shutil.which(name)
        if executable:
            resolved = Path(executable).resolve()
            candidates.append(resolved.parent)
            candidates.extend(_homebrew_roots(resolved))
    candidates.extend(_xcode_roots())
    # Preserve /bin and /lib aliases: ELF interpreters use these literal paths.
    # Never infer read authority from arbitrary PATH directories.
    return tuple(dict.fromkeys(path.absolute() for path in candidates if path.is_dir()
                               and path.resolve() not in {Path.home().resolve(), Path(path.anchor)}))


def _homebrew_roots(executable: Path) -> tuple[Path, ...]:
    """Library trees a Homebrew tool loads from; never its var/ or etc/ state."""
    parts = executable.parts
    if "Cellar" not in parts:
        return ()
    prefix = Path(*parts[:parts.index("Cellar")])
    return tuple(prefix / name for name in ("Cellar", "opt", "lib", "bin", "sbin", "Frameworks", "share"))


def _xcode_roots() -> tuple[Path, ...]:
    """The active developer directory, which the /usr/bin tool shims load from."""
    link = Path("/var/db/xcode_select_link")
    return (link.resolve(),) if link.is_symlink() else ()


def profile_paths(grant: InvocationGrant) -> tuple[list[Path], list[Path]]:
    readable = [Path(path.value) for path in grant.shell_readable]
    writable = [Path(path.value) for path in grant.shell_writable]
    if not readable:
        readable = [Path(target.path.value) for target in grant.targets if target.operation == "file_read"]
    if not writable:
        writable = [Path(target.path.value) for target in grant.targets if target.operation == "file_write"]
    if not readable:
        raise SandboxUnavailable("Shell invocation has no authorized filesystem profile")
    return list(dict.fromkeys(readable)), list(dict.fromkeys(writable))


_GIT_CONTROL_NAMES = ("config", "config.worktree", "hooks")


def git_control_paths(grant: InvocationGrant) -> tuple[Path, ...]:
    """Repository files whose content decides what Git runs.

    Covers the repository gitdir and every submodule gitdir present at launch;
    a submodule initialised by the command itself is not matched again.
    """
    _, writable = profile_paths(grant)
    gitdirs = [gitdir for root in writable for gitdir in _gitdirs(root / ".git")]
    return tuple(dict.fromkeys(gitdir / name for gitdir in gitdirs for name in _GIT_CONTROL_NAMES))


def _gitdirs(gitdir: Path) -> list[Path]:
    if not gitdir.is_dir() or gitdir.is_symlink():
        return []
    found = [gitdir]
    pending = [gitdir / "modules"]
    while pending:
        current = pending.pop()
        if not current.is_dir() or current.is_symlink():
            continue
        if (current / "HEAD").is_file() and (current / "config").is_file():
            # A submodule gitdir; its own submodules sit in its modules/.
            found.append(current)
            pending.append(current / "modules")
            continue
        # Submodule names may contain slashes, so walk intermediate directories.
        pending.extend(child for child in current.iterdir() if child.is_dir())
    return found


def ancestors_within(path: Path, roots: list[Path]) -> tuple[Path, ...]:
    """Directories whose rename would move ``path`` out from under a path rule."""
    return tuple(parent for parent in (path, *path.parents)
                 if any(parent == root or parent.is_relative_to(root) for root in roots))


