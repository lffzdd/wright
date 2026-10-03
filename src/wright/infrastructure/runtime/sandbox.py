"""Platform sandbox launchers. Never fall back to an unrestricted process."""

from __future__ import annotations

import json
import platform
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from ...domain.policy.permission.types import InvocationGrant
from .sandbox_profile import (
    SandboxUnavailable,
    clean_environment,
    profile_paths,
    runtime_roots,
)


def _provider() -> str:
    if sys.platform == "darwin":
        if int(platform.mac_ver()[0].split(".")[0] or 0) < 12:
            raise SandboxUnavailable("Seatbelt requires macOS 12 or newer")
        path = Path("/usr/bin/sandbox-exec")
    elif sys.platform == "linux":
        path = next((candidate for candidate in (Path("/usr/bin/bwrap"), Path("/bin/bwrap")) if candidate.is_file()), Path("/usr/bin/bwrap"))
    else:
        raise SandboxUnavailable("This platform has no Shell isolation backend")
    if not path.is_file() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise SandboxUnavailable(f"A system-owned {path.name} is required; install or enable it")
    return str(path)


@lru_cache(maxsize=1)
def _probe(provider: str) -> tuple[bool, str]:
    argv = ([provider, "-p", "(version 1)(deny default)(allow process-exec)(allow file-read*)", "/usr/bin/true"]
            if sys.platform == "darwin" else
            [provider, "--ro-bind", "/", "/", "--unshare-all", "--cap-drop", "ALL", "--die-with-parent", "--", "/usr/bin/true"])
    try:
        result = subprocess.run(argv, capture_output=True, timeout=5, env=clean_environment(), check=False)
        return result.returncode == 0, result.stderr.decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)


def sandbox_status() -> dict:
    if sys.platform == "win32":
        from .windows_sandbox import status
        return status()
    try:
        available, detail = _probe(_provider())
    except SandboxUnavailable as error:
        available, detail = False, str(error)
    return {"platform": sys.platform, "provider": "Seatbelt" if sys.platform == "darwin" else "Bubblewrap",
            "available": available, "state": "ready" if available else "setup_required", "detail": detail}


def linux_argv(command: str, grant: InvocationGrant, scratch: Path, cwd_file: Path) -> list[str]:
    program = _provider()
    readable, writable = profile_paths(grant)
    argv = [program, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--new-session", "--die-with-parent", "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    if not grant.network_enabled:
        argv += ["--unshare-net"]
    for root in runtime_roots():
        argv += ["--ro-bind", str(root), str(root)]
    for file in ("/etc/ld.so.cache", "/etc/resolv.conf", "/etc/hosts", "/etc/ssl"):
        if Path(file).exists():
            argv += ["--ro-bind", file, file]
    for root in readable:
        argv += ["--ro-bind", str(root), str(root)]
    for root in writable:
        argv += ["--bind", str(root), str(root)]
    for root in runtime_roots():
        argv += ["--ro-bind", str(root), str(root)]
    for root in grant.shell_readonly:
        argv += ["--ro-bind", root.value, root.value]
    hidden = []
    for blocked in sorted(grant.blocked_paths, key=lambda item: len(Path(item.value).parts)):
        path = Path(blocked.value)
        if any(path == root or path.is_relative_to(root) for root in hidden):
            continue
        if any(path == root or path.is_relative_to(root) for root in (*readable, *writable)):
            argv += ["--tmpfs", str(path), "--remount-ro", str(path)] if path.is_dir() else ["--ro-bind", "/dev/null", str(path)]
            hidden.append(path)
    argv += ["--bind", str(scratch), str(scratch), "--chdir", grant.cwd.value]
    import shlex
    script = f"{command}\nwright_exit=$?\npwd -P > {shlex.quote(str(cwd_file))}\nexit $wright_exit"
    return [*argv, "--", "/bin/bash", "--noprofile", "--norc", "-c", script]


def macos_argv(command: str, grant: InvocationGrant, scratch: Path, cwd_file: Path) -> list[str]:
    program = _provider()
    readable, writable = profile_paths(grant)
    def quote(value):
        return json.dumps(str(value), ensure_ascii=True)
    profile = ['(version 1)', '(deny default)', '(allow process-fork process-exec signal)',
               '(allow sysctl-read)', '(allow file-read-metadata)', '(allow mach-lookup (global-name "com.apple.system.logger"))']
    for path in (*runtime_roots(), *readable):
        profile.append(f'(allow file-read* (subpath {quote(path)}))')
    for path in (*writable, scratch):
        profile.append(f'(allow file-read* file-write* (subpath {quote(path)}))')
    for path in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
        profile.append(f'(allow file-read* (literal {quote(path)}))')
    profile.append('(allow file-write* (literal "/dev/null"))')
    for path in runtime_roots():
        profile.append(f'(deny file-write* (subpath {quote(path)}))')
    for path in grant.shell_readonly:
        profile.append(f'(deny file-write* (subpath {quote(path.value)}))')
    for path in grant.blocked_paths:
        profile.append(f'(deny file-read* file-write* (subpath {quote(path.value)}))')
    if grant.network_enabled:
        profile.append('(allow network*)')
    import shlex
    script = f"{command}\nwright_exit=$?\npwd -P > {shlex.quote(str(cwd_file))}\nexit $wright_exit"
    return [program, "-p", "\n".join(profile), "/bin/bash", "--noprofile", "--norc", "-c", script]


def offline_socket_filter() -> bytes:
    """Block new sockets, including pathname Unix sockets exposed by a bind.

    Namespace isolation alone does not prevent accessing host Unix sockets.
    Anonymous socket pairs for child-process IPC stay available.
    """
    architecture = platform.machine().lower()
    definitions = {"x86_64": (0xC000003E, 41), "amd64": (0xC000003E, 41), "aarch64": (0xC00000B7, 198), "arm64": (0xC00000B7, 198)}
    if architecture not in definitions:
        raise SandboxUnavailable("Offline seccomp isolation is unsupported on this CPU")
    audit_arch, socket_syscall = definitions[architecture]
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, audit_arch), (0x06, 0, 0, 0x80000000),
                    (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000),
                    (0x15, 0, 1, socket_syscall), (0x06, 0, 0, 0x00050001),
                    (0x15, 0, 1, 425), (0x06, 0, 0, 0x00050001), (0x06, 0, 0, 0x7FFF0000)]
    return b"".join(struct.pack("<HBBI", *instruction) for instruction in instructions)


# Linux parent-death signals belong to the spawning thread. Tool workers are
# short lived, so all native launches use an owner that lives with the runtime.
_PROCESS_OWNER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wright-sandbox-owner")


def _spawn(argv, cwd, env, *, pass_fds=()):
    return _PROCESS_OWNER.submit(subprocess.Popen, argv, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, bufsize=0,
        start_new_session=True, pass_fds=pass_fds).result()


def launch(command: str, grant: InvocationGrant, scratch: Path, cwd_file: Path):
    if sys.platform == "win32":
        from .windows_sandbox import launch as windows_launch
        return windows_launch(command, grant, scratch, cwd_file)
    argv = macos_argv(command, grant, scratch, cwd_file) if sys.platform == "darwin" else linux_argv(command, grant, scratch, cwd_file)
    env = clean_environment()
    env.update(HOME=str(scratch), TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
    if sys.platform == "linux" and not grant.network_enabled:
        filter_file = scratch / "socket-filter.bpf"
        filter_file.write_bytes(offline_socket_filter())
        with filter_file.open("rb") as descriptor:
            argv[1:1] = ["--seccomp", str(descriptor.fileno())]
            return _spawn(argv, grant.cwd.value, env, pass_fds=(descriptor.fileno(),))
    return _spawn(argv, grant.cwd.value, env)


class LocalSandboxControl:
    def status(self) -> dict:
        _probe.cache_clear()
        return sandbox_status()

    def initialize(self, *, cleanup: bool = False) -> dict:
        if sys.platform == "win32":
            from .windows_sandbox import initialize
            return initialize(cleanup=cleanup)
        _probe.cache_clear()
        return sandbox_status()
