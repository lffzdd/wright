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

from wright.domain.policy.permission.types import InvocationGrant, InvocationIdentity
from wright.infrastructure.runtime.local import LocalExecutionBackend
from wright.infrastructure.runtime.sandbox import sandbox_status
from wright.infrastructure.runtime.types import ExecutionPath

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


def run_script(sandbox, body, *, online=False, timeout=20):
    backend, grant, workspace, *_ = sandbox
    script = workspace / "test.py"
    script.write_text(body, encoding="utf-8")
    text = command(script)
    handle = backend.start_shell(text, cwd=grant.cwd, grant=replace(grant, command=text, subject=text, network_enabled=online))
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
        code, output, _ = run_script(sandbox, body, online=True)
        assert code == 0 and "connected" in output and "file-blocked" in output, output


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
