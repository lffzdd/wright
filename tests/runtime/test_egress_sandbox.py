"""How sandbox launchers confine a command to its egress proxy (no real sandbox)."""

import ast
import socket
import struct
from pathlib import Path

import pytest

from wright.domain.gateway.runtime_types import ExecutionPath
from wright.domain.policy.permission.types import InvocationGrant, InvocationIdentity
from wright.infrastructure.runtime import sandbox


@pytest.fixture
def grant(tmp_path):
    cwd = ExecutionPath("local", str(tmp_path))
    return InvocationGrant(InvocationIdentity("s"), "local", cwd, frozenset({"shell"}), shell_readable=(cwd,),
                           shell_writable=(cwd,), command="true", network_enabled=True)


def test_macos_profile_reaches_only_the_proxy_port(monkeypatch, grant, tmp_path):
    monkeypatch.setattr(sandbox, "_provider", lambda: "/usr/bin/sandbox-exec")
    profile = sandbox.macos_argv("true", grant, tmp_path, tmp_path / "cwd", egress_port=40123)[2]
    assert '(allow network-outbound (remote ip "localhost:40123"))' in profile
    assert "(allow network*)" not in profile and "mDNSResponder" not in profile
    assert profile.count("network") == 1
    legacy = sandbox.macos_argv("true", grant, tmp_path, tmp_path / "cwd")[2]
    assert "(allow network*)" in legacy


def test_linux_proxy_mode_keeps_the_network_namespace_and_runs_the_launcher(monkeypatch, grant, tmp_path):
    monkeypatch.setattr(sandbox, "_provider", lambda: "/usr/bin/bwrap")
    egress = (tmp_path / "egress/proxy.sock", tmp_path / "egress/filter.bpf", tmp_path / "egress/launcher.py")
    argv = sandbox.linux_argv("true", grant, tmp_path, tmp_path / "cwd", egress=egress)
    assert "--unshare-net" in argv and "--seccomp" not in argv
    tail = argv[argv.index("--", argv.index("--chdir")) + 1:]
    assert tail[1:4] == ["-I", "-S", str(egress[2])]
    assert tail[4:7] == [str(sandbox.NAMESPACE_EGRESS_PORT), str(egress[0]), str(egress[1])]
    assert tail[7] == "--" and tail[8] == "/bin/bash"


def test_proxy_mode_never_opens_the_network_without_a_decider(monkeypatch, grant, tmp_path):
    monkeypatch.setattr(sandbox, "egress_mode", lambda: "proxy")
    monkeypatch.setattr(sandbox, "_provider", lambda: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    launched = []
    monkeypatch.setattr(sandbox, "_spawn", lambda argv, cwd, env, **_: launched.append((argv, env)) or "process")
    process, lease = sandbox.launch("true", grant, tmp_path, tmp_path / "cwd")
    argv, env = launched[0]
    assert process == "process" and lease is None
    assert "network" not in argv[2] and "HTTPS_PROXY" not in env


def test_egress_environment_names_the_proxy_for_common_clients():
    env = sandbox.egress_environment(3128)
    for name in ("HTTP_PROXY", "https_proxy", "npm_config_https_proxy"):
        assert env[name] == "http://127.0.0.1:3128"
    assert env["ALL_PROXY"] == "socks5h://127.0.0.1:3128"
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"


def test_egress_mode_switch(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.delenv("WRIGHT_SANDBOX_EGRESS", raising=False)
    assert sandbox.egress_mode() == "proxy"
    monkeypatch.setenv("WRIGHT_SANDBOX_EGRESS", "legacy")
    assert sandbox.egress_mode() == "legacy"
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    monkeypatch.delenv("WRIGHT_SANDBOX_EGRESS")
    assert sandbox.egress_mode() == "legacy"


def run_bpf(program: bytes, *, arch: int, nr: int, arg0: int = 0) -> int:
    """Interpret the classic-BPF subset the filters use."""
    instructions = [struct.unpack("<HBBI", program[i:i + 8]) for i in range(0, len(program), 8)]
    data = {0: nr, 4: arch, 16: arg0}
    pc = accumulator = 0
    while True:
        code, jt, jf, k = instructions[pc]
        if code == 0x20:
            accumulator = data[k]
            pc += 1
        elif code == 0x15:
            pc += 1 + (jt if accumulator == k else jf)
        elif code == 0x35:
            pc += 1 + (jt if accumulator >= k else jf)
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected BPF opcode {code:#x}")


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_egress_seccomp_program(monkeypatch, machine):
    monkeypatch.setattr(sandbox.platform, "machine", lambda: machine)
    arch, socket_nr, ptrace, vm_read, vm_write, io_uring = sandbox._SYSCALLS[machine]
    program = sandbox.egress_socket_filter()
    allow, eperm, kill = 0x7FFF0000, 0x00050001, 0x80000000
    assert run_bpf(program, arch=arch, nr=socket_nr, arg0=socket.AF_INET) == allow
    assert run_bpf(program, arch=arch, nr=socket_nr, arg0=socket.AF_INET6) == allow
    for family in (socket.AF_UNIX, 16, 17):  # unix, netlink, packet
        assert run_bpf(program, arch=arch, nr=socket_nr, arg0=family) == eperm
    for blocked in (ptrace, vm_read, vm_write, io_uring):
        assert run_bpf(program, arch=arch, nr=blocked) == eperm
    assert run_bpf(program, arch=arch, nr=0) == allow
    assert run_bpf(program, arch=0x40000003, nr=socket_nr) == kill
    assert run_bpf(program, arch=arch, nr=0x40000000 + socket_nr) == kill
    # The offline filter still refuses every new socket.
    offline = sandbox.offline_socket_filter()
    assert run_bpf(offline, arch=arch, nr=socket_nr, arg0=socket.AF_INET) == eperm


def test_launcher_is_self_contained():
    source = Path(sandbox.__file__).with_name("sandbox_launcher.py").read_text()
    imports = {alias.name.split(".")[0] for node in ast.walk(ast.parse(source))
               if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
    assert imports <= {"ctypes", "os", "select", "signal", "socket", "sys", "threading"}
