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
            candidates.append(Path(executable).resolve().parent)
    # Preserve /bin and /lib aliases: ELF interpreters use these literal paths.
    # Never infer read authority from arbitrary PATH directories.
    return tuple(dict.fromkeys(path.absolute() for path in candidates if path.is_dir()
                               and path.resolve() not in {Path.home().resolve(), Path(path.anchor)}))


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


