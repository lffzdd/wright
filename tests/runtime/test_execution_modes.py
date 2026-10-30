"""Explicit host mode retains authorization and the process lifecycle."""
import os
import shlex
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from wright.application.permission.checkpoint import SessionCheckpoint
from wright.application.permission.service import PermissionConflict, PermissionService
from wright.application.permission.workflow import AuthorizationWorkflow
from wright.application.session.service import SessionService, SessionServiceError
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.domain.policy.permission.scope import AccessScope
from wright.domain.policy.permission.settings import PermissionSettings
from wright.domain.policy.permission.types import (
    InvocationIdentity,
    PermissionResponse,
    PermissionSubject,
)
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.persistence.session.codec import (
    _deserialize_session,
    _serialize_session,
)
from wright.infrastructure.persistence.session.errors import CheckpointError
from wright.infrastructure.runtime import AuthorizedExecution, LocalExecutionBackend
from wright.infrastructure.tools.command import execute_command_tool


def workflow(mode, approval=lambda request: PermissionResponse("allow_once"), policy="default"):
    return AuthorizationWorkflow(settings=PermissionSettings(mode=policy), approval_handler=approval, execution_mode=mode)


def resolve(flow, backend, command, rules=()):
    tool = execute_command_tool
    return flow.resolve(ToolCall(tool.name, {"command": command}, "call"),
        PermissionSubject(tool.name, tool.requires_user_interaction, tool.describe_access, lambda args: None, tool.approval_details),
        backend=backend, cwd=backend.cwd(), scope=AccessScope(Path(backend.cwd().value)),
        identity=InvocationIdentity("session"), session_rules=rules)


def test_checkpoint_mode_roundtrip_default_and_invalid(tmp_path):
    session = Session.create("goal", tmp_path, execution_mode="host")
    payload = _serialize_session(session)
    assert _deserialize_session(payload).execution_mode == "host"
    del payload["session"]["execution_mode"]
    assert _deserialize_session(payload).execution_mode == "sandbox"
    payload["session"]["execution_mode"] = "invalid"
    with pytest.raises(CheckpointError):
        _deserialize_session(payload)
    with pytest.raises(ValueError):
        Session.create("goal", tmp_path, execution_mode="invalid")


def test_mode_save_rolls_back_and_invalidates_snapshot(tmp_path):
    session = Session.create("goal", tmp_path)
    service = PermissionService(FilePermissionRepository(tmp_path))
    old = service.snapshot(session)
    notifications = []
    service.policy_updated = lambda *args: notifications.append(args)
    class BrokenStore:
        def save(self, session):
            assert session.execution_mode == "host"
            raise OSError("cannot save")
    service.bind_checkpoint(session.session_id, SessionCheckpoint(BrokenStore(), session))
    with pytest.raises(OSError):
        service.set_modes(session, execution_mode="host", permission_mode="bypass")
    assert session.execution_mode == "sandbox"
    assert session.authorization.permission_mode is None
    assert not notifications
    service._checkpoints.clear()
    guard = service.execution_guard(session, old)
    service.set_modes(session, execution_mode="host")
    assert service.snapshot(session).version != old.version
    with pytest.raises(PermissionConflict):
        guard()


def test_host_keeps_approval_and_cannot_reuse_sandbox_command_memory(tmp_path):
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    command = f"{shlex.quote(sys.executable)} -c 'print(42)'"
    remembered = resolve(workflow("sandbox", lambda r: PermissionResponse("allow_session_rule")), backend, command)
    assert remembered.decision == "allow"
    prompts = []
    def deny(request):
        prompts.append(request.prompt)
        return PermissionResponse("deny")
    host = resolve(workflow("host", deny), backend, command, remembered.changes.session_rules)
    assert host.decision == "deny"
    prompt = prompts[0]
    assert prompt.execution_mode == prompt.to_dict()["execution_mode"] == "host"
    assert "allow_once_network" not in {choice.id for choice in prompt.choices}
    assert "No OS filesystem or network isolation" in prompt.shell_note
    for mode in ("host", "sandbox"):
        assert resolve(workflow(mode, policy="plan"), backend, command).decision == "deny"
    allowed = resolve(workflow("host"), backend, command)
    assert allowed.grant.execution_mode == "host"


@pytest.mark.parametrize("busy", ["turn", "approval", "queue", "command", "child", "child_session", "child_lineage"])
def test_switch_requires_entire_tree_idle(tmp_path, busy):
    session = Session.create("goal", tmp_path)
    service = SessionService.__new__(SessionService)
    service._lock = threading.RLock()
    service._queued = {"q": {}} if busy == "queue" else {}
    service._active_command = None
    idle = threading.Event()
    if busy != "turn":
        idle.set()
    if busy == "child_session":
        session.agent_task_id = "child"
    if busy == "child_lineage":
        parent = Session.create("parent", tmp_path, execution_mode="host")
        session.authorization.bind_parent(parent.authorization)
    session.control_plane = SimpleNamespace(has_live_tasks=lambda: busy == "child")
    service.runtime = SimpleNamespace(session_state=session, agent_idle=idle,
        permissions=SimpleNamespace(set_modes=lambda *a, **k: pytest.fail("must reject before saving")),
        runtime_resources=SimpleNamespace(commands=SimpleNamespace(has_live=lambda: busy == "command")),
        directory_coordinator=None, cancellation_event=threading.Event())
    service.runner = SimpleNamespace(state="running")
    service._require_accepting_input = lambda: None
    service._interaction_snapshot = lambda: [{"kind": "permission"}] if busy == "approval" else []
    with pytest.raises(SessionServiceError):
        service.set_execution_policy(execution_mode="host")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX host integration")
def test_host_access_network_cwd_and_process_tree(tmp_path, monkeypatch):
    from wright.infrastructure.runtime import sandbox
    def unavailable(*args):
        pytest.fail("explicit host must not touch the sandbox launcher")
    monkeypatch.setattr(sandbox, "launch", unavailable)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside")
    backend = LocalExecutionBackend(workspace, lambda: workspace)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(3)
    code = f"from pathlib import Path; import socket; p=Path({str(outside)!r}); p.write_text(p.read_text()+'-host'); socket.create_connection(('127.0.0.1',{listener.getsockname()[1]})).close()"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}; cd ..; printf host-ok"
    resolution = resolve(workflow("host"), backend, command)
    handle = AuthorizedExecution(backend, resolution.grant).start_shell(command)
    try:
        connection, _ = listener.accept()
        connection.close()
        assert handle.wait(timeout=5) == 0
        assert handle.cwd_result().value == str(tmp_path.resolve())
        output = b""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            chunk = handle.read_output(65536)
            if chunk is None:
                break
            output += chunk
            time.sleep(.01)
        assert b"host-ok" in output
        assert outside.read_text() == "outside-host"
    finally:
        listener.close()
        handle.terminate(grace_seconds=.1)
    marker = workspace / "child.pid"
    command = f"sleep 60 & echo $! > {shlex.quote(str(marker))}; wait"
    grant = replace(resolution.grant, command=command, subject=command)
    child = AuthorizedExecution(backend, grant).start_shell(command)
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        pid = int(marker.read_text())
        assert child.terminate(grace_seconds=.3)
        assert child.poll() is not None
        assert not child.group_alive()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        child.terminate(grace_seconds=.1)


def test_cli_and_child_inherit_explicit_host_mode(tmp_path, monkeypatch):
    from wright.application.tool_execution.capabilities import (
        assemble_tool_capabilities,
    )
    from wright.interfaces.cli.args import parse_cli_args, runtime_config_from_args
    monkeypatch.setattr(sys, "argv", ["wright", "--execution-mode", "host"])
    assert runtime_config_from_args(parse_cli_args()).execution_mode == "host"
    monkeypatch.setattr(sys, "argv", ["wright", "--execution-mode", "invalid"])
    with pytest.raises(SystemExit):
        parse_cli_args()
    parent = Session.create("parent", tmp_path, execution_mode="host")
    child = Session.create("child", tmp_path)
    assembly = assemble_tool_capabilities(parent, None, None)
    assembly.capabilities.delegation.share_control_plane(child)
    assert child.execution_mode == "host"
    assert child.control_plane is parent.control_plane
    assert assembly.capabilities.delegation.current_execution_mode() == "host"


def test_shell_wrapper_keeps_command_syntax_out_of_the_trailer(tmp_path):
    import subprocess

    from wright.infrastructure.runtime.shell_script import bash_launch_script

    command = "echo 'unterminated $(whoami)"
    script = bash_launch_script(command, tmp_path, tmp_path / "cwd")
    assert "whoami" not in script
    assert "unterminated" not in script
    assert "wright_exit=$?" in script
    assert (tmp_path / "command.sh").read_text() == command + "\n"
    if sys.platform == "win32":
        return
    workspace = tmp_path / "work"
    workspace.mkdir()
    cwd_file = tmp_path / "cwd-out"
    launched = bash_launch_script("cd ..; printf marker", workspace, cwd_file)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", launched],
        cwd=workspace, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "marker"
    assert cwd_file.read_text().strip() == str(tmp_path.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_host_cancellation_escalates_ignored_sigterm(tmp_path):
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    marker = tmp_path / "ready"
    code = f"import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path({str(marker)!r}).touch(); time.sleep(60)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    grant = resolve(workflow("host"), backend, command).grant
    handle = AuthorizedExecution(backend, grant).start_shell(command)
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists()
        assert handle.terminate(grace_seconds=.1)
        assert handle.poll() is not None
        assert not handle.group_alive()
    finally:
        handle.terminate(grace_seconds=.1)
