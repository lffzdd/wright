"""Seatbelt startup and readiness must retain the real invocation boundaries."""

from __future__ import annotations

import json
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from wright.domain.gateway.runtime_types import ExecutionPath
from wright.domain.policy.permission.types import InvocationGrant, InvocationIdentity
from wright.infrastructure.runtime import sandbox, sandbox_profile


@pytest.fixture(autouse=True)
def clear_probe_cache():
    sandbox._probe.cache_clear()
    yield
    sandbox._probe.cache_clear()


def test_macos_probe_runs_real_profile_with_temporary_authority(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "_provider", lambda: "/usr/bin/sandbox-exec")
    original = sandbox.macos_argv
    observed = {}

    def argv(command, grant, scratch, cwd_file):
        observed.update(grant=grant, scratch=scratch, cwd_file=cwd_file)
        return original(command, grant, scratch, cwd_file)

    def run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["environment"] = kwargs["env"]
        observed["command"] = (Path(kwargs["cwd"]) / "command.sh").read_text()
        assert kwargs["cwd"] == observed["scratch"]
        observed["cwd_file"].write_text(str(kwargs["cwd"]) + "\n")
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(sandbox, "macos_argv", argv)
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    assert sandbox._probe("/usr/bin/sandbox-exec") == (True, "")
    arguments = observed["arguments"]
    assert arguments[3:7] == ["/bin/bash", "--noprofile", "--norc", "-c"]
    assert arguments[7].startswith("source ")
    assert "/usr/bin/true" not in arguments[7]
    assert observed["command"].startswith("/usr/bin/true\n")
    assert '(deny default)' in arguments[2]
    assert '(allow file-read-data (literal "/"))' in arguments[2]
    assert '(subpath "/")' not in arguments[2]
    assert '(allow file-read*)' not in arguments[2]
    assert '(allow network*)' not in arguments[2]
    grant = observed["grant"]
    assert grant.shell_readable == grant.shell_writable == (grant.cwd,)
    assert observed["environment"]["HOME"] == str(observed["scratch"])
    assert not observed["scratch"].exists()


def test_macos_profile_resolves_runtime_and_scratch_without_retargeting_grants(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_provider", lambda: "/usr/bin/sandbox-exec")
    roots = {}
    for name in ("runtime", "workspace", "readonly", "scratch"):
        target = tmp_path / (name + "-resolved")
        target.mkdir()
        alias = tmp_path / (name + "-alias")
        alias.symlink_to(target, target_is_directory=True)
        roots[name] = (alias, target)
    workspace_alias, workspace = roots["workspace"]
    protected = workspace / "secret"
    protected.write_text("hidden")

    def path(value):
        return ExecutionPath("local", str(value))

    grant = InvocationGrant(InvocationIdentity("aliases"), "local", path(workspace_alias),
                            frozenset({"shell"}), shell_readable=(path(workspace_alias),),
                            shell_writable=(path(workspace_alias),),
                            shell_readonly=(path(roots["readonly"][0]),),
                            blocked_paths=(path(workspace_alias / "secret"),))
    # An alias swapped after the grant was issued must never acquire new authority.
    unapproved = tmp_path / "unapproved"
    unapproved.mkdir()
    workspace_alias.unlink()
    workspace_alias.symlink_to(unapproved, target_is_directory=True)
    monkeypatch.setattr(sandbox, "runtime_roots", lambda: (roots["runtime"][0],))
    profile = sandbox.macos_argv("/usr/bin/true", grant, roots["scratch"][0], tmp_path / "cwd")[2]
    for name in ("runtime", "scratch"):
        alias, target = roots[name]
        assert str(alias) not in profile
        assert str(target) in profile
    assert f'(allow file-read* (subpath {json.dumps(str(roots["runtime"][1]))}))' in profile
    assert f'(deny file-write* (subpath {json.dumps(str(roots["runtime"][1]))}))' in profile
    assert str(unapproved) not in profile
    assert f'(allow file-read* file-write* (subpath {json.dumps(str(workspace_alias))}))' in profile
    assert f'(deny file-write* (subpath {json.dumps(str(roots["readonly"][0]))}))' in profile
    assert f'(deny file-read* file-write* (subpath {json.dumps(str(workspace_alias / "secret"))}))' in profile


def test_runtime_roots_cover_homebrew_libraries_but_not_its_state(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    tool = prefix / "Cellar" / "tool" / "1.0" / "bin"
    tool.mkdir(parents=True)
    (tool / "git").write_text("")
    for name in ("opt", "lib", "bin", "var", "etc"):
        (prefix / name).mkdir()
    (prefix / "bin" / "git").symlink_to(tool / "git")
    monkeypatch.setattr(sandbox_profile.shutil, "which", lambda name: str(prefix / "bin" / "git") if name == "git" else None)
    roots = sandbox_profile.runtime_roots()
    for name in ("Cellar", "opt", "lib", "bin"):
        assert (prefix / name).resolve() in roots
    assert (prefix / "var").resolve() not in roots
    assert (prefix / "etc").resolve() not in roots
    assert prefix.resolve() not in roots


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt only")
@pytest.mark.parametrize("name", ["python3", "git"])
def test_installed_tools_start_inside_the_real_profile(tmp_path, name):
    executable = shutil.which(name)
    if executable is None:
        pytest.skip(f"{name} is not installed")
    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    path = ExecutionPath("local", str(workspace))
    grant = InvocationGrant(InvocationIdentity("tool"), "local", path, frozenset({"shell"}),
                            shell_readable=(path,), shell_writable=(path,))
    argv = sandbox.macos_argv(f"{shlex.quote(executable)} --version", grant, scratch, scratch / "cwd")
    # The launch script sources command.sh from its working directory.
    (scratch / "command.sh").write_text(f"{shlex.quote(executable)} --version\n")
    done = subprocess.run(argv, cwd=scratch, env=sandbox._sandbox_environment(scratch), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("returncode", [-6, 0])
def test_macos_probe_rejects_crash_or_missing_cwd_writeback(monkeypatch, returncode):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "_provider", lambda: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], returncode, b"", b""))
    status = sandbox.sandbox_status()
    assert not status["available"]
    assert status["state"] == "setup_required"
    assert ("-6" if returncode else "working directory") in status["detail"]


@pytest.mark.native_sandbox
@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration requires macOS")
def test_macos_startup_allowance_preserves_file_and_network_denials(tmp_path):
    import os

    if os.getenv("WRIGHT_SANDBOX_INTEGRATION") != "1":
        pytest.skip("Real native acceptance requires WRIGHT_SANDBOX_INTEGRATION=1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "outside-secret"
    outside.write_text("outside")
    protected = workspace / "protected-secret"
    protected.write_text("protected")

    def path(value):
        return ExecutionPath("local", str(value.resolve()))

    grant = InvocationGrant(InvocationIdentity("startup-regression"), "local", path(workspace),
                            frozenset({"shell"}), shell_readable=(path(workspace),),
                            shell_writable=(path(workspace),), blocked_paths=(path(protected),))
    cwd_file = scratch / "cwd"
    process = sandbox.launch("/usr/bin/true", grant, scratch, cwd_file)
    stdout, _ = process.communicate(timeout=10)
    assert process.returncode == 0, stdout
    assert cwd_file.read_text().strip() == str(workspace.resolve())

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    script = workspace / "boundaries.py"
    script.write_text(f"""import json, socket
from pathlib import Path
results = {{}}
for name, operation in [
    ('root_directory', lambda: list(Path('/').iterdir())),
    ('outside_read', lambda: Path({str(outside)!r}).read_text()),
    ('outside_write', lambda: Path({str(outside)!r}).write_text('changed')),
    ('protected_read', lambda: Path({str(protected)!r}).read_text()),
    ('protected_write', lambda: Path({str(protected)!r}).write_text('changed')),
    ('system_child_read', lambda: Path('/private/etc/passwd').read_text()),
    ('network_connection', lambda: socket.create_connection(('127.0.0.1', {port}), timeout=1)),
]:
    try:
        operation()
        results[name] = True
    except OSError:
        results[name] = False
print(json.dumps(results))
""")
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    process = sandbox.launch(shlex.join([interpreter, str(script)]), grant, scratch, cwd_file)
    try:
        stdout, _ = process.communicate(timeout=10)
    finally:
        listener.close()
    assert process.returncode == 0, stdout
    assert json.loads(stdout) == {
        "root_directory": True,
        "outside_read": False,
        "outside_write": False,
        "protected_read": False,
        "protected_write": False,
        "system_child_read": False,
        "network_connection": False,
    }
    assert outside.read_text() == "outside"
    assert protected.read_text() == "protected"
    assert sandbox.sandbox_status()["available"]


@pytest.mark.native_sandbox
@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration requires macOS")
@pytest.mark.parametrize("choice", ["allow_once", "allow_session_rule", "allow_once_network"])
def test_macos_approved_dispatch_starts_real_shell(tmp_path, monkeypatch, choice):
    import os

    from wright.application.command.execution import CommandExecution
    from wright.application.execution.identity import bind_identity
    from wright.application.permission.workflow import AuthorizationWorkflow
    from wright.application.session_runtime.interaction import RoutedPrompter
    from wright.application.session_runtime.live_resources import RuntimeResources
    from wright.application.session_runtime.publisher import EventPublisher
    from wright.application.tool_execution.approval import InteractiveApprovalHandler
    from wright.application.tool_execution.capabilities import (
        assemble_tool_capabilities,
    )
    from wright.application.tool_execution.dispatch import ToolDispatchService
    from wright.domain.model.session import Session
    from wright.domain.model.tool import ToolCall
    from wright.infrastructure.tools.command import execute_command_tool
    from wright.interfaces.interaction import InteractionBroker

    if os.getenv("WRIGHT_SANDBOX_INTEGRATION") != "1":
        pytest.skip("Real native acceptance requires WRIGHT_SANDBOX_INTEGRATION=1")
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session.create("sandbox-dispatch", workspace)
    identity = bind_identity(session)
    commands = CommandExecution(session, identity)
    resources = RuntimeResources(session.session_id, commands=commands, identity=identity)
    assembly = assemble_tool_capabilities(session, None, resources)
    publisher = EventPublisher(project_id="sandbox-test", session_id=session.session_id)
    broker = InteractionBroker(publisher)
    answers = []

    def respond(event):
        if event.type == "interaction.requested":
            answers.append(broker.resolve(event.payload["request_id"], choice))

    publisher.add_listener(respond)
    dispatch = ToolDispatchService(
        {execute_command_tool.name: execute_command_tool}, assembly, session=session,
        permission_workflow=AuthorizationWorkflow(
            approval_handler=InteractiveApprovalHandler(RoutedPrompter(broker))),
    )
    try:
        result = dispatch.execute([
            ToolCall("execute_command", {"command": "/usr/bin/true"}, "startup"),
        ])[0].result
        assert answers == [True]
        assert result.ok, result
        assert result.data["returncode"] == 0
        assert session.get_cwd() == workspace.resolve()
        assert bool(assembly.permissions.snapshot(session).rules) == (choice == "allow_session_rule")
    finally:
        broker.close()
        commands.close()
