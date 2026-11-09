from pathlib import Path

from tests.permission_helpers import permission_directories
from wright.application.composition.capabilities import assemble_tool_capabilities
from wright.application.permission.workflow import (
    AuthorizationWorkflow,
    _directory_choices,
)
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.domain.gateway.execution import ExecutionPath
from wright.domain.model.session import Session
from wright.domain.model.tool import AccessTarget, ToolAccess, ToolCall
from wright.domain.policy.permission.approval import PermissionResponse, ResolvedTarget
from wright.domain.policy.permission.scope import (
    AccessScope,
    PathClass,
    directory_grant_candidates,
)
from wright.infrastructure.tools.file import read_file_tool, write_file_tool


def _scope(origin="/work/project", protected=()):
    return AccessScope(Path(origin), protected=tuple(Path(p) for p in protected))


def test_candidates_climb_a_few_levels_and_stop_before_top_level():
    assert directory_grant_candidates(("/srv/a/b/c/d",), _scope()) == (
        "/srv/a/b/c/d", "/srv/a/b/c", "/srv/a/b", "/srv/a",
    )
    assert directory_grant_candidates(("/srv/a",), _scope()) == ("/srv/a",)
    # The target's own top-level directory is offered, but never climbed to.
    assert directory_grant_candidates(("/tmp",), _scope()) == ("/tmp",)
    assert directory_grant_candidates(("/",), _scope()) == ()


def test_candidates_never_reach_home_or_its_ancestors():
    home = "/Users/me"
    assert directory_grant_candidates(("/Users/me/lib/src",), _scope(), home=home) == (
        "/Users/me/lib/src", "/Users/me/lib",
    )
    assert directory_grant_candidates(("/Users/me",), _scope(), home=home) == ()


def test_climbing_stops_before_a_directory_holding_protected_files():
    scope = _scope(protected=("/srv/state/permissions.json",))
    assert directory_grant_candidates(("/srv/state/cache/x",), scope) == ("/srv/state/cache/x", "/srv/state/cache")
    # The target's own directory stays available: protected files remain denied inside it.
    assert directory_grant_candidates(("/srv/state",), scope) == ("/srv/state",)


def test_candidates_inside_the_workspace_stop_at_its_root():
    assert directory_grant_candidates(("/work/project/src/pkg",), _scope()) == (
        "/work/project/src/pkg", "/work/project/src", "/work/project",
    )


def test_several_targets_share_their_common_directory():
    assert directory_grant_candidates(("/srv/lib/a", "/srv/lib/b/c"), _scope())[0] == "/srv/lib"


def _target(path, kind="file", operation="file_read"):
    return ResolvedTarget(AccessTarget("file", path, operation, kind=kind), ExecutionPath("local", path), PathClass.OUTSIDE)


def test_multi_file_call_offers_the_common_directory():
    access = ToolAccess(frozenset({"file_read"}))
    choices = _directory_choices(access, (_target("/srv/x/lib/a/x.py"), _target("/srv/x/lib/b/y.py")), _scope())
    nearest = {choice.id: choice for choice in choices}["allow_session_directory_read"]
    assert nearest.scope == "/srv/x/lib"
    assert nearest.resource["kind"] == "directory" and nearest.resource["root"] == "/srv/x/lib"
    assert {"allow_session_directory_read_1", "allow_session_directory_write_1"} <= {choice.id for choice in choices}


def test_risky_or_non_file_access_offers_no_directory():
    targets = (_target("/srv/lib/a/x.py"),)
    assert _directory_choices(ToolAccess(frozenset({"file_write"}), risk_flags=("deletes_files",)), targets, _scope()) == []
    assert _directory_choices(ToolAccess(frozenset({"file_read", "network_read"})), targets, _scope()) == []


def test_write_access_offers_only_read_write_directories():
    access = ToolAccess(frozenset({"file_write"}))
    choices = _directory_choices(access, (_target("/srv/lib/a/x.py", operation="file_write"),), _scope())
    assert choices and all("file_write" in choice.operations for choice in choices)


def test_choosing_a_higher_directory_grants_exactly_that_directory(tmp_path):
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    (shared / "a").mkdir(parents=True)
    (shared / "b").mkdir()
    workspace.mkdir()
    (shared / "b" / "other.txt").write_text("other", encoding="utf-8")
    session = Session.create("ancestor grant", workspace)
    prompts = []

    def approve(request):
        prompts.append(request.prompt)
        return PermissionResponse("allow_session_directory_write_1")

    def executor():
        return ToolDispatchService(
            {write_file_tool.name: write_file_tool, read_file_tool.name: read_file_tool},
            assemble_tool_capabilities(session, None, None, workspace_dir=workspace, cwd_provider=session.get_cwd),
            session=session,
            permission_workflow=AuthorizationWorkflow(approval_handler=approve),
        )

    written = executor().execute([ToolCall("write_file", {"file": str(shared / "a" / "x.txt"), "content": "x"}, "c1")])[0].result
    assert written.ok
    offered = {choice.id: choice.scope for choice in prompts[0].choices}
    assert offered["allow_session_directory_write"] == str((shared / "a").resolve())
    assert offered["allow_session_directory_write_1"] == str(shared.resolve())
    assert permission_directories(session) == (shared.resolve(),)

    read = executor().execute([ToolCall("read_file", {"file": str(shared / "b" / "other.txt")}, "c2")])[0].result
    assert read.ok
    assert len(prompts) == 1
