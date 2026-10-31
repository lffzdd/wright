"""Real dispatcher boundaries, cache invalidation, and crash-recovery regressions."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from wright.application.composition.capabilities import assemble_tool_capabilities
from wright.application.permission.checkpoint import SessionCheckpoint
from wright.application.permission.service import PermissionConflict, PermissionService
from wright.application.permission.workflow import AuthorizationWorkflow
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.core.paths import session_dir, user_permission_settings_path
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.domain.policy.permission.resolver import PermissionResolver
from wright.domain.policy.permission.scope import AccessScope
from wright.domain.policy.permission.types import (
    InvocationIdentity,
    PermissionResolution,
    PermissionResponse,
)
from wright.infrastructure import permission_transaction as transactions
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.runtime import AuthorizedExecution, LocalExecutionBackend
from wright.infrastructure.tools.file import (
    list_directory_tool,
    read_file_tool,
    write_file_tool,
)
from wright.infrastructure.tools.web_tools import http_request_tool, web_search_tool


class CountingRepository(FilePermissionRepository):
    reads = 0
    session_reads = 0

    def read(self):
        self.reads += 1
        return super().read()

    def read_session(self, *args):
        self.session_reads += 1
        return super().read_session(*args)


def setup(root):
    session = Session.create('hardening', root)
    repository = CountingRepository(root)
    service = PermissionService(repository, execution_factory=AuthorizedExecution)
    assembly = assemble_tool_capabilities(session, None, None, permissions=service)
    return session, repository, service, assembly


def dispatcher(session, assembly, tool, choice='allow_once'):
    return ToolDispatchService({tool.name: tool}, assembly, session=session,
        permission_workflow=AuthorizationWorkflow(approval_handler=lambda _: PermissionResponse(choice)))


def test_domain_preparation_never_interacts(tmp_path):
    result = PermissionResolver().prepare(ToolCall('write_file', {'file': 'a', 'content': 'x'}, 'c'), write_file_tool,
        backend=LocalExecutionBackend(tmp_path, lambda: tmp_path), scope=AccessScope(tmp_path), identity=InvocationIdentity('s'))
    assert result.decision == 'ask'
    assert not hasattr(result, 'prompt')


def test_protected_resolution_failure_never_issues_a_grant(tmp_path):
    protected = tmp_path / 'protected'
    class FaultyBackend(LocalExecutionBackend):
        def resolve_path(self, requested, *, cwd=None):
            if requested == str(protected):
                raise OSError('boundary unavailable')
            return super().resolve_path(requested, cwd=cwd)
    result = PermissionResolver().prepare(ToolCall('read_file', {'file': 'a'}, 'c'), read_file_tool,
        backend=FaultyBackend(tmp_path, lambda: tmp_path), scope=AccessScope(tmp_path, protected=(protected,)), identity=InvocationIdentity('s'))
    assert isinstance(result, PermissionResolution)
    assert result.decision == 'deny' and result.grant is None


def test_missing_protected_path_still_enters_the_permit(tmp_path):
    protected = tmp_path / 'missing'
    result = AuthorizationWorkflow().resolve(ToolCall('read_file', {'file': 'a'}, 'c'), read_file_tool,
        backend=LocalExecutionBackend(tmp_path, lambda: tmp_path), scope=AccessScope(tmp_path, protected=(protected,)), identity=InvocationIdentity('s'))
    assert result.grant.blocked_paths[0].value == str(protected)
    assert not protected.exists()


def test_snapshot_is_deeply_immutable(tmp_path):
    session, _, service, _ = setup(tmp_path)
    snapshot = service.snapshot(session)
    with pytest.raises(FrozenInstanceError):
        snapshot.settings.mode = 'bypass'
    with pytest.raises(AttributeError):
        snapshot.settings.allow.append(None)
    service.validate(session, snapshot.version)


@pytest.mark.parametrize('tool,args', [(read_file_tool, {'file': 'a.txt'}), (list_directory_tool, {'directory': '.'})])
def test_resource_checks_do_not_reload_the_whole_repository(tmp_path, tool, args):
    (tmp_path / 'a.txt').write_text('hello')
    for index in range(12):
        (tmp_path / f'item-{index}').write_text('item')
    session, repository, service, assembly = setup(tmp_path)
    service._cache.clear()
    repository.reads = repository.session_reads = 0
    result = dispatcher(session, assembly, tool).execute([ToolCall(tool.name, args, 'c')])[0].result
    assert result.ok
    assert repository.reads <= 2
    assert repository.session_reads <= 2


def test_cached_context_observes_another_service_and_process(tmp_path):
    session, _, service, _ = setup(tmp_path)
    before = service.snapshot(session)
    script = '''import sys
from pathlib import Path
from wright.application.permission.service import PermissionService
from wright.domain.model.session import Session
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.runtime import LocalExecutionBackend
root=Path(sys.argv[1]); session=Session.create('other',root)
service=PermissionService(FilePermissionRepository(root))
service.grant_resource(session,LocalExecutionBackend(root,session.get_cwd),{'kind':'file','path':'other.txt','operations':['file_read']},'project',service.snapshot(session).version)
'''
    subprocess.run([sys.executable, '-c', script, str(tmp_path)], check=True)
    with pytest.raises(PermissionConflict):
        service.validate(session, before.version)
    other = PermissionService(FilePermissionRepository(tmp_path))
    current = other.snapshot(session)
    rule = current.settings.project_rules[0]
    other.revoke(session, rule.id, 'project', current.version)
    with pytest.raises(PermissionConflict):
        service.validate(session, current.version)


def test_network_permit_binds_target_method_and_query(tmp_path, monkeypatch):
    sent = []
    def transport(method, url, **kwargs):
        sent.append((method, url, kwargs))
        return httpx.Response(200, json={'ok': True}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx, 'request', transport)
    session, _, service, assembly = setup(tmp_path)
    result = dispatcher(session, assembly, http_request_tool).execute([ToolCall('http_request', {'url': 'https://example.test/path', 'params': {'q': 'one'}}, 'c')])[0].result
    assert result.ok and len(sent) == 1
    assert sent[0][2]['follow_redirects'] is False
    from wright.domain.policy.permission.types import PermissionSubject
    invocation = service.authorize(session, ToolCall('http_request', {'url': 'https://example.test/path', 'params': {'q': 'one'}}, 'c2'),
        PermissionSubject('http_request', False, http_request_tool.describe_access, lambda _: None), backend=assembly.backend,
        workflow=AuthorizationWorkflow(approval_handler=lambda _: PermissionResponse('allow_once')), requires_network=True)
    for method, url, params in [('POST', 'https://example.test/path', {'q': 'one'}), ('GET', 'https://other.test/path', {'q': 'one'}), ('GET', 'https://example.test/path', {'q': 'two'})]:
        with pytest.raises(PermissionError):
            invocation.network.request(method, url, operation='network_read', params=params)
    with pytest.raises(PermissionError):
        invocation.network.request('GET', 'https://example.test/path', operation='network_read', params={'q': 'one'}, follow_redirects=True)
    service.set_modes(session, interaction_mode='ask')
    with pytest.raises(PermissionConflict):
        invocation.network.request('GET', 'https://example.test/path', operation='network_read', params={'q': 'one'})
    assert len(sent) == 1


def test_changed_network_arguments_are_rejected_at_send(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, 'request', lambda *a, **k: calls.append(a))
    def changed(args, runtime):
        args['params']['q'] = 'changed'
        return http_request_tool.call(args, runtime)
    tool = replace(http_request_tool, call=changed)
    session, _, _, assembly = setup(tmp_path)
    result = dispatcher(session, assembly, tool).execute([ToolCall('http_request', {'url': 'https://example.test', 'params': {'q': 'original'}}, 'c')])[0].result
    assert not result.ok and not calls


def test_search_post_keeps_read_semantics_and_credentials_out_of_grants(tmp_path, monkeypatch):
    sent = []
    secret = 'test-provider-credential'
    monkeypatch.setenv('TAVILY_API_KEY', secret)
    def transport(method, url, **kwargs):
        sent.append((method, kwargs))
        return httpx.Response(200, json={'results': []}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx, 'request', transport)
    session, _, service, assembly = setup(tmp_path)
    result = dispatcher(session, assembly, web_search_tool).execute([ToolCall('web_search', {'query': 'query', 'max_results': 2}, 'c')])[0].result
    assert result.ok and sent[0][0] == 'POST'
    assert sent[0][1]['headers']['Authorization'] == f'Bearer {secret}'
    assert secret not in repr(service.snapshot(session))
    assert secret not in user_permission_settings_path().read_text()


def initial_files(root):
    session, repository, service, _ = setup(root)
    store = FileSessionRepository(session_dir(root))
    store.save(session)
    checkpoint = store.path_for(session.session_id)
    config = user_permission_settings_path()
    return session, repository, service, store, config, checkpoint


CRASH_SCRIPT = '''import json, os, sys
from pathlib import Path
from wright.infrastructure import permission_transaction as tx
config, checkpoint = Path(sys.argv[1]), Path(sys.argv[2]); phase=sys.argv[3]
original_log=tx._write_log; original_replace=tx._replace; writes=0
def log(path,data):
    original_log(path,data)
    if phase == data['state']:
        os._exit(71)
def replace(path,content):
    global writes
    original_replace(path,content)
    if path in {config,checkpoint}:
        writes+=1
        if phase == str(writes): os._exit(71)
tx._write_log=log; tx._replace=replace
with tx.unit_of_work():
    settings=json.loads(config.read_bytes());settings['mode']='acceptEdits'
    payload=json.loads(checkpoint.read_bytes());payload['session']['session_label']='committed'
    tx.atomic_write(config,json.dumps(settings).encode())
    tx.atomic_write(checkpoint,json.dumps(payload).encode())
'''


@pytest.mark.parametrize('phase', ['PREPARED', '1', '2', 'COMMITTED'])
def test_process_exit_recovers_all_participants_idempotently(tmp_path, phase):
    _, _, _, _, config, checkpoint = initial_files(tmp_path)
    before = (config.read_bytes(), checkpoint.read_bytes())
    result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(config), str(checkpoint), phase])
    assert result.returncode == 71
    assert list(transactions.journal_directory().glob('*.json'))
    for _ in range(2):
        with transactions.transaction():
            pass
    if phase == 'COMMITTED':
        assert json.loads(config.read_bytes())['mode'] == 'acceptEdits'
        assert json.loads(checkpoint.read_bytes())['session']['session_label'] == 'committed'
    else:
        assert (config.read_bytes(), checkpoint.read_bytes()) == before
    assert not list(transactions.journal_directory().glob('*.json'))


def test_ordinary_checkpoint_save_recovers_before_later_write(tmp_path):
    session, _, _, store, config, checkpoint = initial_files(tmp_path)
    result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(config), str(checkpoint), 'COMMITTED'])
    assert result.returncode == 71
    session.session_label = 'later'
    store.save(session)
    with transactions.transaction():
        pass
    assert store.load(session.session_id).session_label == 'later'
    assert json.loads(config.read_bytes())['mode'] == 'acceptEdits'


def test_recovery_attempts_every_compensation_and_blocks_authority(tmp_path, monkeypatch):
    session, _, service, _, config, checkpoint = initial_files(tmp_path)
    subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(config), str(checkpoint), '2'], check=False)
    original = transactions._replace
    attempted = []
    def failing(path, content):
        attempted.append(path)
        if path == config:
            raise OSError('config recovery failed')
        return original(path, content)
    monkeypatch.setattr(transactions, '_replace', failing)
    with pytest.raises(transactions.PermissionRecoveryError):
        service.snapshot(session)
    assert config in attempted and checkpoint in attempted
    assert list(transactions.journal_directory().glob('*.json'))
    monkeypatch.setattr(transactions, '_replace', original)
    assert service.snapshot(session).mode == 'default'


def test_unregistered_journal_target_fails_closed(tmp_path):
    session, _, service, _, _, _ = initial_files(tmp_path)
    outside = tmp_path / 'outside.json'
    outside.write_text('keep')
    directory = transactions.journal_directory()
    directory.mkdir()
    (directory / 'invalid.json').write_text(json.dumps({'version': 1, 'state': 'COMMITTED', 'files': [
        {'path': str(outside), 'before': transactions._encoded(b'keep'), 'after': transactions._encoded(b'overwrite')}
    ]}))
    with pytest.raises(transactions.PermissionRecoveryError):
        service.snapshot(session)
    assert outside.read_text() == 'keep'


def test_checkpoint_and_authorization_commit_roll_back_together(tmp_path, monkeypatch):
    session, _, service, store, config, checkpoint = initial_files(tmp_path)
    service.bind_checkpoint(session.session_id, SessionCheckpoint(store, session))
    before = (config.read_bytes(), checkpoint.read_bytes())
    original = transactions._replace
    def failed_checkpoint(path, content):
        if path == checkpoint and content != before[1]:
            raise OSError('checkpoint write failed')
        return original(path, content)
    monkeypatch.setattr(transactions, '_replace', failed_checkpoint)
    with pytest.raises(OSError):
        service.grant_resource(session, LocalExecutionBackend(tmp_path, session.get_cwd),
            {'kind': 'file', 'path': 'new.txt', 'operations': ['file_read']}, 'user', service.snapshot(session).version)
    assert (config.read_bytes(), checkpoint.read_bytes()) == before
    assert not service.snapshot(session).settings.allow


def test_capture_retries_when_a_file_changes_after_full_read(tmp_path, monkeypatch):
    session, repository, service, _ = setup(tmp_path)
    service._cache.clear()
    original = repository.read
    changed = False
    def racing_read():
        nonlocal changed
        result = original()
        if not changed:
            changed = True
            path = user_permission_settings_path()
            settings = json.loads(path.read_bytes())
            settings['mode'] = 'plan'
            settings['revision'] = 'external-write'
            path.write_text(json.dumps(settings))
        return result
    monkeypatch.setattr(repository, 'read', racing_read)
    snapshot = service.snapshot(session)
    assert snapshot.mode == 'plan'
    assert service.snapshot(session) is snapshot


def test_unrelated_session_does_not_invalidate_in_memory_context(tmp_path):
    from wright.domain.policy.permission.settings import PermissionSettings
    from wright.infrastructure.config.memory_permissions import (
        MemoryPermissionRepository,
    )
    repository = MemoryPermissionRepository(PermissionSettings(), tmp_path)
    service = PermissionService(repository)
    first = Session.create('first', tmp_path)
    second = Session.create('second', tmp_path)
    snapshot = service.snapshot(first)
    service.snapshot(second)
    assert service.snapshot(first) is snapshot


def test_checkpoint_writer_waits_for_recovery_and_preserves_later_write(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    session, _, _, store, config, checkpoint = initial_files(tmp_path)
    crashed = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(config), str(checkpoint), 'COMMITTED'])
    assert crashed.returncode == 71
    recovering, resume, writing = Event(), Event(), Event()
    original = transactions._recover_log
    def paused_recovery(path):
        recovering.set()
        assert resume.wait(5)
        original(path)
    monkeypatch.setattr(transactions, '_recover_log', paused_recovery)
    def recover():
        with transactions.transaction():
            pass
    def save():
        writing.set()
        session.session_label = 'concurrent-later'
        store.save(session)
    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery = pool.submit(recover)
        assert recovering.wait(5)
        writer = pool.submit(save)
        assert writing.wait(5)
        assert not writer.done()
        resume.set()
        recovery.result(timeout=5)
        writer.result(timeout=5)
    assert store.load(session.session_id).session_label == 'concurrent-later'
    assert json.loads(config.read_bytes())['mode'] == 'acceptEdits'


def test_corrupt_journal_checksum_blocks_all_authority(tmp_path):
    session, _, service, _, config, _ = initial_files(tmp_path)
    before = config.read_bytes()
    invalid = transactions._encoded(b'bad')
    invalid['sha256'] = 'incorrect'
    directory = transactions.journal_directory()
    directory.mkdir()
    log = directory / 'corrupt.json'
    log.write_text(json.dumps({'version': 1, 'state': 'COMMITTED', 'files': [
        {'path': str(config), 'before': transactions._encoded(before), 'after': invalid}
    ]}))
    with pytest.raises(transactions.PermissionRecoveryError, match='checksum'):
        service.snapshot(session)
    assert log.exists() and config.read_bytes() == before


def test_protection_revalidated_before_shell_launch(tmp_path):
    from wright.domain.gateway.runtime_types import ExecutionPath
    from wright.domain.policy.permission.types import InvocationGrant
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    protected = tmp_path / 'protected'
    protected.mkdir()
    grant = InvocationGrant(InvocationIdentity('s', call_id='c'), backend.environment_id,
        backend.cwd(), frozenset({'shell'}), command='true', subject='true',
        blocked_paths=(backend.resolve_path(str(protected)),))
    execution = AuthorizedExecution(backend, grant)
    protected.rmdir()
    protected.symlink_to(tmp_path / 'changed', target_is_directory=True)
    with pytest.raises(PermissionError, match='resource changed'):
        execution.start_shell('true')
    assert grant.blocked_paths == (ExecutionPath(backend.environment_id, str(protected)),)


def test_authorization_change_rejects_mutable_and_mismatched_rules():
    from wright.domain.policy.permission.settings import PermissionRule
    from wright.domain.policy.permission.types import AuthorizationChange
    with pytest.raises(TypeError, match='typed'):
        AuthorizationChange(session_rules=({'kind': 'tool'},))
    with pytest.raises(TypeError, match='immutable'):
        AuthorizationChange(session_rules=(PermissionRule('read_file', operations=['file_read']),))
    with pytest.raises(ValueError, match='lifetime'):
        AuthorizationChange(project_rules=(PermissionRule('read_file', lifetime='user'),))
