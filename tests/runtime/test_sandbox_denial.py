"""Sandbox denial hints explain failures without changing any authority."""

from __future__ import annotations

import pytest

from wright.application.command.sandbox_denial import (
    describe_sandbox_denial,
    sandbox_denial_message,
)
from wright.domain.gateway.runtime_types import ExecutionPath
from wright.domain.policy.permission.types import InvocationGrant, InvocationIdentity


def grant(tmp_path, mode="sandbox"):
    root = ExecutionPath("local", str(tmp_path.resolve()))
    return InvocationGrant(InvocationIdentity("denial"), "local", root, frozenset({"shell"}),
                           shell_readable=(root,), shell_writable=(root,), execution_mode=mode)


@pytest.mark.parametrize("line", [
    "ls: {path}: Operation not permitted",
    "PermissionError: [Errno 1] Operation not permitted: '{path}'",
])
def test_outside_path_suggests_its_directory(tmp_path, line):
    outside = tmp_path.parent / "outside-dir"
    outside.mkdir(exist_ok=True)
    denial = describe_sandbox_denial(line.format(path=outside), grant(tmp_path))
    assert denial["path"] == str(outside.resolve())
    assert denial["grant_directory"] == str(outside.resolve())
    assert denial["readable"] == [str(tmp_path.resolve())]
    assert "not by command approval" in sandbox_denial_message(denial)


def test_outside_file_suggests_parent_and_root_is_never_suggested(tmp_path):
    outside = tmp_path.parent / "outside-file"
    outside.write_text("x")
    denial = describe_sandbox_denial(f"cat: {outside}: Operation not permitted", grant(tmp_path))
    assert denial["grant_directory"] == str(tmp_path.parent.resolve())
    assert describe_sandbox_denial("ls: /: Operation not permitted", grant(tmp_path))["grant_directory"] == ""


def test_other_failures_are_not_blamed_on_the_sandbox(tmp_path):
    inside = tmp_path / "file"
    assert describe_sandbox_denial("Tests: 1 failed", grant(tmp_path)) is None
    assert describe_sandbox_denial(f"rm: {inside}: Operation not permitted", grant(tmp_path)) is None
    assert describe_sandbox_denial("ls: /x: Operation not permitted", grant(tmp_path, "host")) is None
    assert describe_sandbox_denial("ls: /x: Operation not permitted", None) is None


def test_unparsed_denial_still_explains_without_a_suggestion(tmp_path):
    denial = describe_sandbox_denial("Operation not permitted", grant(tmp_path))
    assert denial["path"] == denial["grant_directory"] == ""


def test_outside_path_also_offers_a_read_write_grant(tmp_path):
    outside = tmp_path.parent / "outside-write"
    outside.mkdir(exist_ok=True)
    denial = describe_sandbox_denial(f"touch: {outside / 'new'}: Operation not permitted", grant(tmp_path))
    assert denial["grant_directory"] == denial["grant_write_directory"] == str(outside.resolve())


def test_write_into_a_read_only_directory_offers_only_read_write(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    root = ExecutionPath("local", str(workspace.resolve()))
    extra = ExecutionPath("local", str(library.resolve()))
    read_only = InvocationGrant(InvocationIdentity("denial"), "local", root, frozenset({"shell"}),
                                shell_readable=(root, extra), shell_writable=(root,), execution_mode="sandbox")
    denial = describe_sandbox_denial(f"touch: {library / 'x'}: Read-only file system", read_only)
    assert denial["grant_directory"] == ""
    assert denial["grant_write_directory"] == str(library.resolve())


def test_write_grant_is_never_suggested_for_home_or_top_level(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    denial = describe_sandbox_denial(f"touch: {tmp_path.parent / 'x'}: Operation not permitted", grant(tmp_path))
    assert denial["grant_directory"] == str(tmp_path.parent.resolve())
    assert denial["grant_write_directory"] == ""
    assert describe_sandbox_denial("touch: /zz-missing: Operation not permitted", grant(tmp_path))["grant_write_directory"] == ""
