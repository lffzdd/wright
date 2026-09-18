import threading
import time

import pytest

from wright.tests.responses import event, response

from ...application_host import ApplicationHost
from ...autonomy import AutonomyStore, AutonomyStoreError, TriggerSpec
from ...executor import ToolExecutor
from ...permission import PermissionSettings
from ...tools.base import Tool, ToolCall


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def __call__(self, _messages, **_kwargs):
        yield event(response(content=self.answer, calls=[]))


def _store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, AutonomyStore(
        tmp_path / "tasks.sqlite3", session_id="origin-session", workspace_dir=workspace
    )


def test_command_acceptance_is_durable_and_rejects_changed_replay(tmp_path):
    _workspace, store = _store(tmp_path)
    _first, created = store.accept_command("project/session", "cmd-1", {"op": "submit"}, now=1)
    replay, created_again = store.accept_command("project/session", "cmd-1", {"op": "submit"}, now=2)

    assert created is True
    assert created_again is False
    assert replay["status"] == "accepted"
    with pytest.raises(AutonomyStoreError, match="different content"):
        store.accept_command("project/session", "cmd-1", {"op": "cancel"}, now=3)
    store.complete_command("project/session", "cmd-1", {"run_id": "run-1"}, now=4)
    store.close()

    reopened = AutonomyStore(tmp_path / "tasks.sqlite3", session_id="origin-session", workspace_dir=tmp_path / "workspace")
    assert reopened.get_command("project/session", "cmd-1")["result"] == {"run_id": "run-1"}
    reopened.close()


def test_recovery_marks_started_tool_effect_unknown_without_replay(tmp_path):
    _workspace, store = _store(tmp_path)
    automation = store.create_automation(
        name="once", prompt="work", trigger=TriggerSpec(type="once", run_at=0), now=0
    )
    run_id = store.materialize_due(now=0)[0]
    store.claim_next_run(owner_id="host-a", now=0)
    store.start_run(run_id, owner_id="host-a", now=0)
    store.record_tool_intent(
        run_id=run_id, call_id="call-1", tool_name="write_file",
        arguments={"path": "out.txt", "content": "x"}, permission={"decision": "allow"},
        environment={"workspace_dir": "workspace"}, now=1,
    )
    store.mark_tool_started(run_id, "call-1", now=1)
    store.close()

    reopened = AutonomyStore(tmp_path / "tasks.sqlite3", session_id="origin-session", workspace_dir=tmp_path / "workspace")
    recovered = reopened.recover_interrupted(now=2)
    assert recovered[0].status == "unknown"
    assert reopened.list_tool_executions(run_id)[0]["status"] == "unknown"
    assert reopened.get_automation(automation.id).id == automation.id
    reopened.close()


def test_recovery_does_not_retry_a_run_with_an_unconfirmed_effect(tmp_path):
    _workspace, store = _store(tmp_path)
    store.create_automation(
        name="retry", prompt="write", trigger=TriggerSpec(type="once", run_at=0),
        recovery_policy="retry", max_retries=2, now=0,
    )
    run_id = store.materialize_due(now=0)[0]
    store.claim_next_run(owner_id="host-a", now=0)
    store.start_run(run_id, owner_id="host-a", now=0)
    store.record_tool_intent(
        run_id=run_id, call_id="call-1", tool_name="write_file",
        arguments={"path": "out.txt"}, permission={"decision": "allow"},
        environment={"workspace_dir": "workspace"}, now=0,
    )
    store.mark_tool_started(run_id, "call-1", now=0)
    store.close()

    reopened = AutonomyStore(
        tmp_path / "tasks.sqlite3", session_id="origin-session", workspace_dir=tmp_path / "workspace"
    )
    assert reopened.recover_interrupted(now=1)[0].status == "unknown"
    assert reopened.get_run(run_id).status == "unknown"
    reopened.close()


def test_application_host_runs_persisted_automation_without_source_session(tmp_path):
    workspace, store = _store(tmp_path)
    automation = store.create_automation(
        name="headless", prompt="finish unattended", trigger=TriggerSpec(type="once", run_at=0),
        run_config={"max_steps": 3, "profile": "durable"}, now=0,
    )
    host = ApplicationHost(
        workspace_dir=workspace,
        store=store,
        llm=ScriptLLM("headless result"),
        base_tools=[],
        permission_settings=PermissionSettings(),
        poll_interval=0.01,
    )
    host.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        rows = store.list_runs(automation.id)
        if rows and rows[0].terminal:
            break
        time.sleep(0.01)
    assert rows[0].status == "completed"
    assert rows[0].result == "headless result"
    assert host.snapshot().state == "running"
    host.close()
    assert host.state == "closed"


def test_run_config_is_a_constrained_snapshot_not_a_permission_grant(tmp_path):
    _workspace, store = _store(tmp_path)
    with pytest.raises(AutonomyStoreError, match="only support the durable profile"):
        store.create_automation(
            name="bad", prompt="work", trigger=TriggerSpec(type="once", run_at=0),
            run_config={"profile": "main"}, now=0,
        )
    automation = store.create_automation(
        name="configured", prompt="work", trigger=TriggerSpec(type="once", run_at=0),
        run_config={"profile": "durable", "model": "fake", "max_steps": 2}, now=0,
    )
    run_id = store.materialize_due(now=0)[0]
    assert store.get_run(run_id).run_config == automation.run_config
    store.close()


def test_second_application_host_cannot_recover_a_live_owner(tmp_path):
    workspace, store = _store(tmp_path)
    first = ApplicationHost(
        workspace_dir=workspace, store=store, llm=ScriptLLM("done"), base_tools=[],
        permission_settings=PermissionSettings(),
    )
    first.start()
    second_store = AutonomyStore(
        tmp_path / "tasks.sqlite3", session_id="origin-session", workspace_dir=workspace
    )
    second = ApplicationHost(
        workspace_dir=workspace, store=second_store, llm=ScriptLLM("done"), base_tools=[],
        permission_settings=PermissionSettings(),
    )
    with pytest.raises(RuntimeError, match="already owns"):
        second.start()
    assert second.state == "closed"
    first.close()


def test_host_defers_store_close_until_its_worker_actually_exits(tmp_path):
    workspace, store = _store(tmp_path)
    host = ApplicationHost(
        workspace_dir=workspace, store=store, llm=ScriptLLM("done"), base_tools=[],
        permission_settings=PermissionSettings(),
    )
    host.start()
    started, release = threading.Event(), threading.Event()
    task = host.control_plane.begin_task(
        root_turn_id="close-test", parent_id=None, tool_call_id="close-test", depth=1,
        task="wait", requested_steps=1,
    )

    def block() -> None:
        started.set()
        release.wait(timeout=2)

    host.background.submit(task.id, block, host.control_plane)
    assert started.wait(timeout=1)
    assert host.close(grace_seconds=0) is False
    assert host.state == "closing"
    assert not store.closed
    release.set()
    assert host._closed.wait(timeout=2)
    assert host.state == "closed"
    assert store.closed


def test_effect_is_not_called_when_intent_cannot_be_persisted(tmp_path):
    called: list[dict] = []

    class FailingJournal:
        def record_intent(self, **_value):
            raise OSError("disk full")

        def mark_started(self, _call_id):
            raise AssertionError("must not start after failed intent")

    tool = Tool("effect", "effect", {"type": "object"}, lambda args, _rt: called.append(args))
    executor = ToolExecutor({"effect": tool}, workspace_dir=tmp_path, execution_journal=FailingJournal())

    outcome = executor.execute([ToolCall("effect", {"target": "x"}, "call-1")])[0]

    assert not outcome.result.ok
    assert "intent persistence failed" in outcome.result.err
    assert called == []
