"""Real native isolation acceptance. CI enables this suite; no mocked launcher."""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from wright.domain.gateway.runtime_types import ExecutionPath
from wright.domain.policy.permission.types import InvocationGrant, InvocationIdentity
from wright.infrastructure.runtime.local import LocalExecutionBackend
from wright.infrastructure.runtime.sandbox import egress_mode, sandbox_status

pytestmark = pytest.mark.native_sandbox


@pytest.fixture
def sandbox(tmp_path):
    if os.getenv("WRIGHT_SANDBOX_INTEGRATION") != "1":
        pytest.skip("Real native acceptance requires WRIGHT_SANDBOX_INTEGRATION=1")
    readiness = sandbox_status()
    assert readiness["available"], readiness
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = workspace / "protected"
    protected.mkdir()
    (protected / "secret").write_text("protected-secret", encoding="utf-8")
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    (readonly / "data").write_text("readable", encoding="utf-8")
    backend = LocalExecutionBackend(workspace, lambda: workspace)
    def path(value):
        return ExecutionPath("local", str(value.resolve()))
    grant = InvocationGrant(InvocationIdentity("native"), "local", path(workspace), frozenset({"shell"}),
                            shell_readable=(path(workspace), path(readonly)), shell_writable=(path(workspace),),
                            shell_readonly=(path(readonly),), blocked_paths=(path(protected), path(protected / "secret")))
    return backend, grant, workspace, outside, protected, readonly


def command(script: Path) -> str:
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    if sys.platform == "win32":
        return "& '" + interpreter.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'"
    return shlex.join([interpreter, str(script)])


class AllowHosts:
    """Egress decider for acceptance runs: these hosts, private ones included."""

    def __init__(self, *hosts):
        self.hosts = set(hosts)
        self.revoked = False

    def decide(self, host, port, *, private):
        return host in self.hosts and not self.revoked

    def still_allowed(self, host, port, *, private):
        return host in self.hosts and not self.revoked


def start_script(sandbox, body, *, online=False, egress=None):
    backend, grant, workspace, *_ = sandbox
    script = workspace / "test.py"
    script.write_text(body, encoding="utf-8")
    text = command(script)
    options = {"egress": egress} if egress is not None else {}
    return backend.start_shell(text, cwd=grant.cwd, grant=replace(grant, command=text, subject=text, network_enabled=online), **options)


def run_script(sandbox, body, *, online=False, timeout=20, egress=None, handle_out=None):
    handle = start_script(sandbox, body, online=online, egress=egress)
    if handle_out is not None:
        handle_out.append(handle)
    chunks = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            chunk = handle.read_output(65536)
            if chunk is None:
                break
            if chunk:
                chunks.append(chunk)
            assert time.monotonic() < deadline, b"".join(chunks)
            time.sleep(0.01)
        code = handle.wait(timeout=5)
        return code, b"".join(chunks).decode("utf-8", errors="replace"), handle.cwd_result()
    finally:
        handle.terminate(grace_seconds=0)


@pytest.mark.parametrize("resource", ["outside", "protected", "readonly"])
def test_reads_and_writes_obey_native_boundaries(sandbox, resource):
    _, _, workspace, outside, protected, readonly = sandbox
    root = {"outside": outside, "protected": protected, "readonly": readonly}[resource]
    source = root / ("secret" if resource == "protected" else "data")
    if not source.exists():
        source.write_text("outside-secret", encoding="utf-8")
    destination = root / "created"
    body = f"""from pathlib import Path
import json
result = {{}}
for name, operation in [('read', lambda: Path({str(source)!r}).read_text()), ('write', lambda: Path({str(destination)!r}).write_text('escape'))]:
    try:
        operation()
        result[name] = True
    except OSError:
        result[name] = False
print(json.dumps(result))
"""
    code, output, _ = run_script(sandbox, body)
    assert code == 0, output
    result = json.loads(output.strip().splitlines()[-1])
    assert result == {"read": resource == "readonly", "write": False}, output
    assert not destination.exists()
    assert (workspace / "test.py").exists()


def test_symlink_or_junction_cannot_escape(sandbox):
    _, _, workspace, outside, *_ = sandbox
    (outside / "data").write_text("secret", encoding="utf-8")
    link = workspace / "link"
    if sys.platform == "win32":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    code, output, _ = run_script(sandbox, f"from pathlib import Path\ntry:\n print(Path({str(link / 'data')!r}).read_text())\nexcept OSError:\n print('blocked')\n")
    assert code == 0 and "blocked" in output and "secret" not in output


def test_socket_capability_is_independent_of_file_isolation(sandbox):
    _, _, _, outside, *_ = sandbox
    secret = outside / "secret"
    secret.write_text("hidden", encoding="utf-8")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        body = f"""import socket
from pathlib import Path
try:
    connection = socket.create_connection(('127.0.0.1', {port}), timeout=2)
    connection.close()
    print('connected')
except OSError:
    print('offline')
try:
    print(Path({str(secret)!r}).read_text())
except OSError:
    print('file-blocked')
"""
        code, output, _ = run_script(sandbox, body)
        assert code == 0 and "offline" in output and "file-blocked" in output, output
        if egress_mode() == "legacy":
            code, output, _ = run_script(sandbox, body, online=True)
            assert code == 0 and "connected" in output and "file-blocked" in output, output
        else:
            # Even an approved command never connects directly; see the egress tests.
            code, output, _ = run_script(sandbox, body, online=True, egress=AllowHosts("127.0.0.1"))
            assert code == 0 and "offline" in output and "file-blocked" in output, output


def test_git_node_python_and_cwd(sandbox):
    _, _, workspace, *_ = sandbox
    (workspace / "nested").mkdir()
    code, output, cwd = run_script(sandbox, "import subprocess, sys\nprint(sys.version)\nsubprocess.run(['git', 'init', '-q'], check=True)\nsubprocess.run(['node', '-e', 'console.log(42)'], check=True)\n")
    assert code == 0 and "42" in output, output
    assert (workspace / ".git").is_dir()
    assert Path(cwd.value) == workspace
    backend, grant, *_ = sandbox
    text = "Set-Location nested" if sys.platform == "win32" else "cd nested"
    handle = backend.start_shell(text, cwd=grant.cwd, grant=replace(grant, command=text, subject=text))
    try:
        handle.wait(timeout=10)
        assert Path(handle.cwd_result().value) == workspace / "nested"
    finally:
        handle.terminate(grace_seconds=0)


def test_git_control_files_stay_read_only_including_submodules(sandbox):
    _, _, workspace, *_ = sandbox
    gitdirs = (workspace / ".git", workspace / ".git" / "modules" / "lib" / "sub")
    for gitdir in gitdirs:
        (gitdir / "hooks").mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (gitdir / "config").write_text("[core]\n", encoding="utf-8")
    targets = [str(gitdir / name) for gitdir in gitdirs for name in ("config", "hooks/pre-commit")]
    others = [str(gitdir / "index") for gitdir in gitdirs]
    body = f"""
for path in {targets!r}:
    try:
        open(path, 'a').write('x')
        print('wrote', path)
    except OSError:
        print('blocked')
for path in {others!r}:
    open(path, 'w').write('index')
    print('other-ok')
"""
    code, output, _ = run_script(sandbox, body)
    assert code == 0 and "wrote" not in output, output
    assert output.count("blocked") == 4 and output.count("other-ok") == 2, output
    assert (gitdirs[1] / "config").read_text(encoding="utf-8") == "[core]\n"


def test_cancel_kills_derived_background_processes(sandbox):
    backend, grant, workspace, *_ = sandbox
    script = workspace / "background.py"
    marker = workspace / "late"
    child = f"import time; time.sleep(4); open({str(marker)!r}, 'w').write('alive')"
    script.write_text(f"import subprocess,sys,time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\nprint('started', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    text = command(script)
    handles = []
    worker = threading.Thread(target=lambda: handles.append(backend.start_shell(text, cwd=grant.cwd, grant=replace(grant, command=text, subject=text))))
    worker.start()
    worker.join(timeout=15)
    assert not worker.is_alive() and handles
    handle = handles[0]
    deadline = time.monotonic() + 15
    output = b""
    while b"started" not in output:
        output += handle.read_output(65536) or b""
        assert time.monotonic() < deadline, output
        time.sleep(0.01)
    handle.terminate(grace_seconds=0)
    assert not handle.group_alive()
    time.sleep(4.2)
    assert not marker.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bind mounts also expose pathname Unix sockets")
def test_offline_blocks_host_unix_socket_in_authorized_directory(sandbox):
    _, _, workspace, *_ = sandbox
    path = workspace / "host.socket"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen(2)
        code, output, _ = run_script(sandbox, f"import socket\ntry:\n connection=socket.socket(socket.AF_UNIX); connection.connect({str(path)!r}); print('connected')\nexcept OSError:\n print('blocked')\n")
        assert code == 0 and "blocked" in output and "connected" not in output, output


PROXY_CLIENT = """import os, socket
def via_proxy(host, port):
    proxy = os.environ.get("WRIGHT_EGRESS_PROXY", "")
    if not proxy:
        return "no-proxy"
    address, proxy_port = proxy.rsplit("/", 1)[-1].split(":")
    try:
        connection = socket.create_connection((address, int(proxy_port)), timeout=5)
        connection.sendall(f"CONNECT {host}:{port} HTTP/1.1\\r\\n\\r\\n".encode())
        status = connection.recv(64).split(b" ")[1].decode()
        if status == "200":
            connection.sendall(b"ping")
            status += ":" + connection.recv(4).decode()
        connection.close()
        return status
    except OSError as error:
        return "error:" + type(error).__name__
def direct(host, port):
    try:
        socket.create_connection((host, port), timeout=2).close()
        return "connected"
    except OSError:
        return "blocked"
"""


@pytest.fixture
def echo_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def serve():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                data = connection.recv(4)
                connection.sendall(data)
    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


proxy_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps the all-or-nothing network switch")


@proxy_only
def test_egress_proxy_is_the_only_way_out(sandbox, echo_server):
    assert egress_mode() == "proxy"
    body = PROXY_CLIENT + f"""
print("proxy-allowed", via_proxy("127.0.0.1", {echo_server}))
print("proxy-denied", via_proxy("example.com", 443))
print("direct", direct("127.0.0.1", {echo_server}))
try:
    socket.getaddrinfo("example.com", 443)
    print("dns resolved")
except OSError:
    print("dns blocked")
"""
    handles = []
    code, output, _ = run_script(sandbox, body, egress=AllowHosts("127.0.0.1"), handle_out=handles)
    assert code == 0, output
    assert "proxy-allowed 200:ping" in output, output
    assert "proxy-denied 403" in output and "direct blocked" in output and "dns blocked" in output, output
    assert {"host": "example.com", "port": 443, "reason": "not allowed"} in handles[0].network_denials()


@proxy_only
def test_without_a_decider_the_command_is_offline(sandbox, echo_server):
    body = PROXY_CLIENT + f"print('proxy', via_proxy('127.0.0.1', {echo_server}))\nprint('direct', direct('127.0.0.1', {echo_server}))\n"
    code, output, _ = run_script(sandbox, body, online=True)
    assert code == 0 and "proxy no-proxy" in output and "direct blocked" in output, output


@proxy_only
def test_closed_proxy_means_no_network(sandbox, echo_server):
    marker = sandbox[2] / "go"
    body = PROXY_CLIENT + f"""import time, pathlib
print("before", via_proxy("127.0.0.1", {echo_server}), flush=True)
while not pathlib.Path({str(marker)!r}).exists():
    time.sleep(0.05)
print("after", via_proxy("127.0.0.1", {echo_server}))
"""
    handle = start_script(sandbox, body, egress=AllowHosts("127.0.0.1"))
    try:
        output = b""
        deadline = time.monotonic() + 20
        while b"before" not in output:
            output += handle.read_output(65536) or b""
            assert time.monotonic() < deadline, output
            time.sleep(0.01)
        handle._egress.close()
        marker.write_text("1")
        while (chunk := handle.read_output(65536)) is not None:
            output += chunk
            assert time.monotonic() < deadline, output
            time.sleep(0.01)
        text = output.decode()
        assert "before 200:ping" in text and "after 200" not in text, text
    finally:
        handle.terminate(grace_seconds=0)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux bridge and seccomp confinement")
def test_linux_bridge_confinement(sandbox):
    body = """import ctypes, os, socket
for name, family in (("unix", socket.AF_UNIX), ("netlink", 16)):
    try:
        socket.socket(family, socket.SOCK_RAW if family == 16 else socket.SOCK_STREAM).close()
        print(name, "allowed")
    except OSError:
        print(name, "blocked")
forwarders = [pid for pid in os.listdir("/proc") if pid.isdigit() and pid != str(os.getpid())
              and b"launcher.py" in open(f"/proc/{pid}/cmdline", "rb").read()]
print("forwarders", len(forwarders))
libc = ctypes.CDLL(None, use_errno=True)
for pid in forwarders:
    print("ptrace", libc.ptrace(16, int(pid), None, None))
    try:
        open(f"/proc/{pid}/mem", "wb").close()
        print("mem writable")
    except OSError:
        print("mem blocked")
"""
    code, output, _ = run_script(sandbox, body, egress=AllowHosts())
    assert code == 0, output
    assert "unix blocked" in output and "netlink blocked" in output, output
    assert "forwarders 1" in output and "ptrace -1" in output and "mem blocked" in output, output
