"""Isolation startup failures cannot run an ordinary fallback process."""
from dataclasses import replace

import pytest

from wright.domain.policy.permission.types import InvocationGrant, InvocationIdentity
from wright.infrastructure.runtime.local import LocalExecutionBackend
from wright.infrastructure.runtime.sandbox import SandboxUnavailable


def test_unavailable_sandbox_never_executes_the_command(tmp_path, monkeypatch):
    import wright.infrastructure.runtime.sandbox as sandbox
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    cwd = backend.cwd()
    grant = InvocationGrant(InvocationIdentity("test"), "local", cwd, frozenset({"shell"}), shell_readable=(cwd,), shell_writable=(cwd,))
    def unavailable(*_):
        raise SandboxUnavailable("Isolation unavailable")
    monkeypatch.setattr(sandbox, "launch", unavailable)
    command = "echo escaped > fallback.txt"
    with pytest.raises(SandboxUnavailable):
        backend.start_shell(command, cwd=cwd, grant=replace(grant, command=command, subject=command))
    assert not (tmp_path / "fallback.txt").exists()
