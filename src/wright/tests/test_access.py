import shlex

import pytest

from ..access import AccessScope, PathClass, forbidden_paths
from ..domain.session import Session
from ..execution import LocalExecutionBackend
from ..tools.base import tool_runtime_for_session
from ..tools.command_tools import execute_command


def test_access_scope_classifies_origin_granted_and_outside(tmp_path):
    origin = tmp_path / "workspace"
    extra = tmp_path / "extra"
    other = tmp_path / "other"
    origin.mkdir()
    extra.mkdir()
    other.mkdir()

    scope = AccessScope(origin, [extra])

    assert scope.classify(origin / "a.txt") == PathClass.IN_ORIGIN
    assert scope.classify(extra / "nested" / "b.txt") == PathClass.IN_GRANTED
    assert scope.classify(other / "c.txt") == PathClass.OUTSIDE
    assert scope.contains(extra / "nested")
    assert not scope.contains(other)


def test_access_scope_add_root_is_idempotent(tmp_path):
    origin = tmp_path / "workspace"
    extra = tmp_path / "extra"
    origin.mkdir()
    extra.mkdir()
    scope = AccessScope(origin)

    scope.add(extra)
    scope.add(extra / "nested")
    scope.add(origin / "inside")

    assert scope.additional == (extra.resolve(),)
    assert scope.classify(extra / "nested" / "a.txt") == PathClass.IN_GRANTED


def test_backend_accepts_granted_roots_and_call_scoped_paths(tmp_path):
    origin = tmp_path / "workspace"
    extra = tmp_path / "extra"
    origin.mkdir()
    extra.mkdir()
    outside_file = extra / "a.txt"
    sibling = tmp_path / "other" / "b.txt"
    sibling.parent.mkdir()

    backend = LocalExecutionBackend(origin, lambda: origin)

    with pytest.raises(ValueError, match="Unsafe path"):
        backend.path(str(outside_file))

    backend.set_invocation_paths([outside_file])
    assert backend.path(str(outside_file)) == outside_file.resolve()
    backend.clear_invocation_paths()
    with pytest.raises(ValueError, match="Unsafe path"):
        backend.path(str(outside_file))

    backend.access.add(extra)
    assert backend.path(str(outside_file)) == outside_file.resolve()
    with pytest.raises(ValueError, match="Unsafe path"):
        backend.path(str(sibling))


def test_backend_rejects_forbidden_permission_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    settings = forbidden_paths()[0]
    settings.write_text("{}", encoding="utf-8")
    origin = tmp_path / "workspace"
    origin.mkdir()
    backend = LocalExecutionBackend(origin, lambda: origin)

    assert backend.classify_path(str(settings))[1] == PathClass.FORBIDDEN
    with pytest.raises(ValueError, match="Unsafe path"):
        backend.path(str(settings))


def test_display_path_is_relative_in_origin_and_absolute_outside(tmp_path):
    origin = tmp_path / "workspace"
    extra = tmp_path / "extra"
    origin.mkdir()
    extra.mkdir()
    backend = LocalExecutionBackend(origin, lambda: origin, additional=[extra])

    assert backend.display_path(origin / "src" / "a.py") == "src/a.py"
    assert backend.display_path(extra / "a.py") == str((extra / "a.py").resolve())


def test_command_cwd_snaps_back_when_leaving_granted_roots(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session.create("snap", workspace)
    runtime = tool_runtime_for_session(session, workspace_dir=workspace)

    result = execute_command("cd ..", runtime=runtime)

    assert result.ok
    assert session.get_cwd() == workspace
    assert result.data["cwd"] == "."
    assert result.data["cwd_reset"] is True


def test_command_cwd_stays_inside_granted_extra_root(tmp_path):
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    session = Session.create("keep extra cwd", workspace)
    session.add_working_directory(extra)
    runtime = tool_runtime_for_session(session, workspace_dir=workspace)

    result = execute_command(f"cd {shlex.quote(str(extra))}", runtime=runtime)

    assert result.ok
    assert session.get_cwd() == extra.resolve()
    assert result.data["cwd"] == str(extra.resolve())
    assert "cwd_reset" not in result.data
