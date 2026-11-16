"""Shell egress decisions issued and remembered through the permission service."""

import pytest

from wright.application.permission.service import PermissionService
from wright.application.permission.workflow import AuthorizationWorkflow
from wright.application.workspace.grants import list_grants
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.domain.policy.permission.types import PermissionResponse
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.runtime import AuthorizedExecution, LocalExecutionBackend
from wright.infrastructure.tools.command import execute_command_tool


@pytest.fixture
def boundary(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    session = Session.create("egress", root)
    service = PermissionService(FilePermissionRepository(root), execution_factory=AuthorizedExecution)
    return session, service, LocalExecutionBackend(root, session.get_cwd)


def authorize(boundary, answers, **arguments):
    session, service, backend = boundary
    prompts = []

    def approve(request):
        prompts.append(request.prompt)
        return PermissionResponse(answers.pop(0))
    call = ToolCall("shell", {"command": "true", **arguments}, "call")
    invocation = service.authorize(session, call, execute_command_tool, backend=backend, requires_execution=True,
                                   workflow=AuthorizationWorkflow(approval_handler=approve))
    assert invocation.execution is not None
    return invocation.execution._egress, prompts


def test_host_answer_is_saved_as_a_shared_host_rule(boundary):
    session, service, _ = boundary
    egress, prompts = authorize(boundary, ["allow_once", "allow_session_network_host"])
    assert egress.decide("pypi.org", 443, private=False)
    assert prompts[-1].operation == "network_connect"
    rows = [row for row in list_grants(session, service)["grants"] if row["resource_kind"] == "network"]
    assert rows and rows[0]["target"] == "pypi.org:443"
    later, later_prompts = authorize(boundary, ["allow_once"])
    assert later.decide("pypi.org", 443, private=False) and len(later_prompts) == 1


def test_background_commands_are_never_asked(boundary):
    egress, prompts = authorize(boundary, ["allow_once"], run_in_background=True)
    assert not egress.decide("pypi.org", 443, private=False) and len(prompts) == 1


def test_command_network_approval_covers_public_hosts_only(boundary):
    egress, _prompts = authorize(boundary, ["allow_once_network"], network=True)
    assert egress.decide("pypi.org", 443, private=False)
    egress.end_interaction()
    assert not egress.decide("10.0.0.2", 443, private=True)
