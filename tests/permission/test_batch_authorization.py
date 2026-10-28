"""Issued permits survive unrelated grants but not changes to restrictions."""

import json

import httpx
import pytest

from wright.application.permission.service import PermissionConflict, PermissionService
from wright.application.permission.workflow import AuthorizationWorkflow
from wright.application.session.interaction import RoutedPrompter
from wright.application.session.publisher import EventPublisher
from wright.application.tool_execution.approval import InteractiveApprovalHandler
from wright.application.tool_execution.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.domain.policy.permission.types import PermissionResponse, PermissionSubject
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.runtime.network import AuthorizedNetwork
from wright.infrastructure.tools.web_tools import http_request_tool
from wright.interfaces.interaction import InteractionBroker


def setup_network(tmp_path):
    session = Session.create('batch', tmp_path)
    repository = FilePermissionRepository(tmp_path)
    sent = []

    def transport(method, url, **kwargs):
        sent.append((method, url))
        return httpx.Response(200, json={'ok': True}, request=httpx.Request(method, url))

    service = PermissionService(repository, network_factory=lambda grant, validator: AuthorizedNetwork(
        grant, validator=validator, transport=transport))
    assembly = assemble_tool_capabilities(session, None, None, permissions=service)
    return session, repository, service, assembly, sent


@pytest.mark.parametrize('choice', ['allow_once', 'allow_session_rule', 'allow_project_rule'])
def test_two_approved_network_calls_in_one_batch(tmp_path, choice):
    session, _, service, assembly, sent = setup_network(tmp_path)
    publisher = EventPublisher(project_id='project', session_id=session.session_id)
    broker = InteractionBroker(publisher)
    answers = []

    def respond(event):
        if event.type == 'interaction.requested':
            answers.append(broker.resolve(event.payload['request_id'], choice))

    publisher.add_listener(respond)
    dispatch = ToolDispatchService(
        {http_request_tool.name: http_request_tool}, assembly, session=session,
        permission_workflow=AuthorizationWorkflow(
            approval_handler=InteractiveApprovalHandler(RoutedPrompter(broker))),
    )
    results = dispatch.execute([
        ToolCall('http_request', {'url': 'https://one.example/info'}, 'one'),
        ToolCall('http_request', {'url': 'https://two.example/info'}, 'two'),
    ])
    assert answers == [True, True]
    assert all(item.result.ok for item in results), [item.result for item in results]
    assert set(sent) == {('GET', 'https://one.example/info'), ('GET', 'https://two.example/info')}
    assert len(sent) == 2
    if choice != 'allow_once':
        assert len(service.snapshot(session).rules if choice == 'allow_session_rule'
                   else service.snapshot(session).settings.project_rules) == 2


def issue(session, service, assembly, choice='allow_session_rule'):
    return service.authorize(
        session, ToolCall('http_request', {'url': 'https://one.example/info', 'params': {'q': 'one'}}, 'one'),
        PermissionSubject('http_request', False, http_request_tool.describe_access, lambda _: None),
        backend=assembly.backend,
        workflow=AuthorizationWorkflow(approval_handler=lambda _: PermissionResponse(choice)),
        requires_network=True,
    )


@pytest.mark.parametrize('change', ['revoke', 'deny', 'mode', 'scope'])
def test_restrictions_invalidate_issued_network_permit_before_transport(tmp_path, change):
    session, repository, service, assembly, sent = setup_network(tmp_path)
    invocation = issue(session, service, assembly)
    snapshot = service.snapshot(session)
    if change == 'revoke':
        service.revoke(session, snapshot.rules[0].id, 'session', snapshot.version)
    elif change == 'deny':
        repository.project_path.parent.mkdir(parents=True, exist_ok=True)
        repository.project_path.write_text(json.dumps({'version': 2, 'mode': 'default',
            'permissions': {'deny': ['http_request']}}))
    elif change == 'mode':
        service.set_modes(session, interaction_mode='ask')
    else:
        session.project_root = tmp_path / 'other'
    with pytest.raises(PermissionConflict):
        invocation.network.request('GET', 'https://one.example/info', operation='network_read', params={'q': 'one'})
    assert not sent


def test_addition_preserves_permit_but_not_target_method_or_query(tmp_path):
    session, _, service, assembly, sent = setup_network(tmp_path)
    invocation = issue(session, service, assembly)
    service.grant_resource(session, assembly.backend,
        {'kind': 'file', 'path': 'other.txt', 'operations': ['file_read']},
        'project', service.snapshot(session).version)
    for method, url, params in [
        ('POST', 'https://one.example/info', {'q': 'one'}),
        ('GET', 'https://two.example/info', {'q': 'one'}),
        ('GET', 'https://one.example/info', {'q': 'two'}),
    ]:
        with pytest.raises(PermissionError):
            invocation.network.request(method, url, operation='network_read', params=params)
    invocation.network.request('GET', 'https://one.example/info', operation='network_read', params={'q': 'one'})
    assert sent == [('GET', 'https://one.example/info')]
