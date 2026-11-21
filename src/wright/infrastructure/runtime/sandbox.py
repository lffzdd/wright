"""Platform sandbox launchers. Never fall back to an unrestricted process."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from ...domain.gateway.runtime_protocols import EgressDecider
from ...domain.gateway.runtime_types import ExecutionPath
from ...domain.policy.permission.types import InvocationGrant, InvocationIdentity
from .egress_proxy import open_tcp_lease, open_unix_lease
from .sandbox_profile import (
    SandboxUnavailable,
    ancestors_within,
    clean_environment,
    git_control_paths,
    profile_paths,
    runtime_roots,
)
from .shell_script import bash_launch_script

# Inside Linux's private network namespace nothing else listens, so the
# forwarder to the egress proxy can use one fixed loopback port.
NAMESPACE_EGRESS_PORT = 3128
_LOCAL_DESTINATIONS = "localhost,127.0.0.1,::1"


def egress_mode(configured: str = "proxy") -> str:
    """``proxy``: network only through the per-command egress proxy.

    ``legacy`` keeps the earlier all-or-nothing switch (``sandbox.egress``);
    Windows has no proxy mode yet and always uses it. Anything but ``legacy``
    is the proxy, the stricter of the two.
    """
    if sys.platform not in {"darwin", "linux"}:
        return "legacy"
    return "legacy" if configured.strip().lower() == "legacy" else "proxy"


def egress_environment(port: int) -> dict[str, str]:
    """Proxy settings common clients read. Clients that ignore them stay offline."""
    http = f"http://127.0.0.1:{port}"
    socks = f"socks5h://127.0.0.1:{port}"
    env = {}
    for name, value in (("HTTP_PROXY", http), ("HTTPS_PROXY", http), ("ALL_PROXY", socks),
                        ("NO_PROXY", _LOCAL_DESTINATIONS)):
        env[name] = env[name.lower()] = value
    env.update(npm_config_proxy=http, npm_config_https_proxy=http, NODE_USE_ENV_PROXY="1",
               WRIGHT_EGRESS_PROXY=http)
    return env


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
    try:
        if sys.platform == "darwin":
            # Exercise the same shell, profile and cwd writeback as a real launch.
            # This temporary authority never enters a session or permission store.
            with TemporaryDirectory(prefix="wright-sandbox-probe-") as temporary:
                scratch = Path(temporary).resolve()
                cwd = ExecutionPath("local", str(scratch))
                grant = InvocationGrant(InvocationIdentity("sandbox-probe"), "local", cwd,
                                        frozenset({"shell"}), shell_readable=(cwd,), shell_writable=(cwd,))
                cwd_file = scratch / "cwd"
                argv = macos_argv("/usr/bin/true", grant, scratch, cwd_file)
                result = subprocess.run(argv, cwd=scratch, capture_output=True, timeout=5,
                                        env=_sandbox_environment(scratch), check=False)
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                if result.returncode:
                    return False, detail or f"Sandbox shell exited with status {result.returncode}"
                if not cwd_file.is_file() or cwd_file.read_text().strip() != str(scratch):
                    return False, "Sandbox shell did not write its working directory"
                return True, detail
        argv = [provider, "--ro-bind", "/", "/", "--unshare-all", "--cap-drop", "ALL", "--die-with-parent", "--", "/usr/bin/true"]
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


def linux_argv(command: str, grant: InvocationGrant, scratch: Path, cwd_file: Path,
               egress: tuple[Path, Path, Path] | None = None) -> list[str]:
    """``egress`` is (proxy socket, seccomp program, launcher) when the proxy carries the network."""
    program = _provider()
    readable, writable = profile_paths(grant)
    argv = [program, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--new-session", "--die-with-parent", "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    if egress is not None or not grant.network_enabled:
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
    for path in git_control_paths(grant):
        if path.exists():
            argv += ["--ro-bind", str(path), str(path)]
    hidden = []
    for blocked in sorted(grant.blocked_paths, key=lambda item: len(Path(item.value).parts)):
        path = Path(blocked.value)
        if any(path == root or path.is_relative_to(root) for root in hidden):
            continue
        if any(path == root or path.is_relative_to(root) for root in (*readable, *writable)):
            argv += ["--tmpfs", str(path), "--remount-ro", str(path)] if path.is_dir() else ["--ro-bind", "/dev/null", str(path)]
            hidden.append(path)
    argv += ["--bind", str(scratch), str(scratch), "--chdir", grant.cwd.value]
    script = bash_launch_script(command, scratch, cwd_file)
    shell = ["/bin/bash", "--noprofile", "--norc", "-c", script]
    if egress is not None:
        socket_path, filter_path, launcher = egress
        shell = [_interpreter(), "-I", "-S", str(launcher), str(NAMESPACE_EGRESS_PORT), str(socket_path),
                 str(filter_path), "--", *shell]
    return [*argv, "--", *shell]


def _interpreter() -> str:
    # The real interpreter, not a venv shim: its prefix is a read-only runtime root.
    return os.path.realpath(sys.executable)


def macos_argv(command: str, grant: InvocationGrant, scratch: Path, cwd_file: Path,
               egress_port: int | None = None) -> list[str]:
    program = _provider()
    readable, writable = profile_paths(grant)
    def quote(value):
        return json.dumps(str(value), ensure_ascii=True)
    # Seatbelt matches canonical paths. Resolve trusted runtime installations and
    # private scratch, preserving the immutable paths issued by authorization.
    runtime = tuple(path.resolve() for path in runtime_roots())
    scratch = scratch.resolve()
    profile = ['(version 1)', '(deny default)', '(allow process-fork process-exec)',
               '(allow signal (target same-sandbox))', '(allow sysctl-read)', '(allow file-read-metadata)', '(allow mach-lookup (global-name "com.apple.system.logger"))']
    # dyld's libignition opens the root directory to locate the system cryptex.
    # A literal match allows that directory (including listing), never its children.
    profile.append('(allow file-read-data (literal "/"))')
    for path in (*runtime, *readable):
        profile.append(f'(allow file-read* (subpath {quote(path)}))')
    for path in (*writable, scratch):
        profile.append(f'(allow file-read* file-write* (subpath {quote(path)}))')
    for path in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
        profile.append(f'(allow file-read* (literal {quote(path)}))')
    # System TLS configuration and CA certificates (Linux binds /etc/ssl too);
    # without them TLS clients abort before any connection.
    profile.append('(allow file-read* (subpath "/private/etc/ssl"))')
    profile.append('(allow file-write* (literal "/dev/null"))')
    for path in runtime:
        profile.append(f'(deny file-write* (subpath {quote(path)}))')
    for path in grant.shell_readonly:
        profile.append(f'(deny file-write* (subpath {quote(path.value)}))')
    for path in git_control_paths(grant):
        profile.append(f'(deny file-write* (subpath {quote(path)}))')
    for path in grant.blocked_paths:
        profile.append(f'(deny file-read* file-write* (subpath {quote(path.value)}))')
    # Seatbelt rules follow paths, so a renamed parent would carry a boundary
    # out of its rule. Children of these directories remain writable.
    bounded = (*(Path(item.value) for item in (*grant.shell_readonly, *grant.blocked_paths)), *git_control_paths(grant))
    parents = dict.fromkeys(parent for path in bounded for parent in ancestors_within(path.parent, [*writable, scratch]))
    for path in parents:
        profile.append(f'(deny file-write* (literal {quote(path)}))')
    if egress_port is not None:
        # The egress proxy's listener for this command is the only reachable
        # endpoint; there is no DNS or other socket access.
        profile.append(f'(allow network-outbound (remote ip "localhost:{int(egress_port)}"))')
    elif grant.network_enabled:
        profile.append('(allow network*)')
    script = bash_launch_script(command, scratch, cwd_file)
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


_SYSCALLS = {
    # audit arch, socket, ptrace, process_vm_readv, process_vm_writev, io_uring_setup
    "x86_64": (0xC000003E, 41, 101, 310, 311, 425), "amd64": (0xC000003E, 41, 101, 310, 311, 425),
    "aarch64": (0xC00000B7, 198, 117, 270, 271, 425), "arm64": (0xC00000B7, 198, 117, 270, 271, 425),
}
_KILL, _EPERM, _ALLOW = 0x80000000, 0x00050001, 0x7FFF0000


def egress_socket_filter() -> bytes:
    """Seccomp program for commands whose network is the egress proxy.

    New sockets may only be AF_INET/AF_INET6, which in the private network
    namespace reach nothing but ``lo`` (and the forwarder on it). Unix,
    netlink and packet sockets, io_uring, ptrace and cross-process memory
    access stay blocked, so the command can neither reach host sockets nor
    take over the unconfined forwarder.
    """
    architecture = platform.machine().lower()
    if architecture not in _SYSCALLS:
        raise SandboxUnavailable("Seccomp isolation is unsupported on this CPU")
    audit_arch, socket_nr, *blocked = _SYSCALLS[architecture]
    head = [(0x20, 0, 0, 4), (0x15, 1, 0, audit_arch), (0x06, 0, 0, _KILL),
            (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, _KILL)]
    body = []
    for number in blocked:
        body += [(0x15, 0, 1, number), (0x06, 0, 0, _EPERM)]
    body.append((0x06, 0, 0, _ALLOW))
    # socket(): load args[0] (the address family) and allow only inet families.
    socket_check = [(0x20, 0, 0, 16), (0x15, 2, 0, socket.AF_INET), (0x15, 1, 0, socket.AF_INET6),
                    (0x06, 0, 0, _EPERM), (0x06, 0, 0, _ALLOW)]
    jump = [(0x15, len(body), 0, socket_nr)]
    instructions = [*head, *jump, *body, *socket_check]
    return b"".join(struct.pack("<HBBI", *instruction) for instruction in instructions)


# Linux parent-death signals belong to the spawning thread. Tool workers are
# short lived, so all native launches use an owner that lives with the runtime.
_PROCESS_OWNER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wright-sandbox-owner")


def _spawn(argv, cwd, env, *, pass_fds=()):
    return _PROCESS_OWNER.submit(subprocess.Popen, argv, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, bufsize=0,
        start_new_session=True, pass_fds=pass_fds).result()


def _sandbox_environment(scratch: Path) -> dict[str, str]:
    env = clean_environment()
    # Homebrew git reads <prefix>/etc/gitconfig, which stays outside the sandbox on purpose.
    env.update(HOME=str(scratch), TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch), GIT_CONFIG_NOSYSTEM="1")
    return env


def launch(
    command: str, grant: InvocationGrant, scratch: Path, cwd_file: Path, *,
    egress: EgressDecider | None = None, egress_setting: str = "proxy",
):
    """Start the command; returns ``(process, egress lease or None)``.

    In proxy mode the command reaches the network only through a lease that
    ``egress`` decides for; without a decider it runs offline.
    """
    if sys.platform == "win32":
        from .windows_sandbox import launch as windows_launch
        return windows_launch(command, grant, scratch, cwd_file), None
    if egress_mode(egress_setting) == "proxy":
        return _launch_with_egress(command, grant, scratch, cwd_file, egress)
    argv = macos_argv(command, grant, scratch, cwd_file) if sys.platform == "darwin" else linux_argv(command, grant, scratch, cwd_file)
    env = _sandbox_environment(scratch)
    if sys.platform == "linux" and not grant.network_enabled:
        filter_file = scratch / "socket-filter.bpf"
        filter_file.write_bytes(offline_socket_filter())
        with filter_file.open("rb") as descriptor:
            argv[1:1] = ["--seccomp", str(descriptor.fileno())]
            return _spawn(argv, grant.cwd.value, env, pass_fds=(descriptor.fileno(),)), None
    return _spawn(argv, grant.cwd.value, env), None


def _launch_with_egress(command: str, grant: InvocationGrant, scratch: Path, cwd_file: Path, decider: EgressDecider | None):
    # In proxy mode ``network_enabled`` only tells the decider that the command
    # was approved for every public host; the sandbox itself never opens up.
    grant = replace(grant, network_enabled=False)
    env = _sandbox_environment(scratch)
    lease = None
    try:
        if sys.platform == "darwin":
            port = None
            if decider is not None:
                lease = open_tcp_lease(decider)
                port = lease.port
                env.update(egress_environment(port))
            argv = macos_argv(command, grant, scratch, cwd_file, egress_port=port)
            return _spawn(argv, grant.cwd.value, env), lease
        bridge = scratch / "egress"
        bridge.mkdir(mode=0o700)
        filter_path = bridge / "socket-filter.bpf"
        if decider is None:
            # Offline: the same launcher and confinement, with nothing behind the bridge.
            filter_path.write_bytes(offline_socket_filter())
            with filter_path.open("rb") as descriptor:
                argv = linux_argv(command, grant, scratch, cwd_file)
                if "--unshare-net" not in argv:
                    argv[1:1] = ["--unshare-net"]
                argv[1:1] = ["--seccomp", str(descriptor.fileno())]
                return _spawn(argv, grant.cwd.value, env, pass_fds=(descriptor.fileno(),)), None
        filter_path.write_bytes(egress_socket_filter())
        launcher = bridge / "launcher.py"
        shutil.copyfile(Path(__file__).with_name("sandbox_launcher.py"), launcher)
        lease = open_unix_lease(decider, bridge / "proxy.sock")
        env.update(egress_environment(NAMESPACE_EGRESS_PORT))
        argv = linux_argv(command, grant, scratch, cwd_file, egress=(bridge / "proxy.sock", filter_path, launcher))
        return _spawn(argv, grant.cwd.value, env), lease
    except BaseException:
        if lease is not None:
            lease.close()
        raise


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
