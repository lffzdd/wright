import queue
import threading

import pytest

from ...autonomy import (
    AutonomyScheduler,
    AutonomyStore,
    AutonomyStoreError,
    TriggerSpec,
)
from ...autonomy.triggers import probe_public_web_page
from ...session import SessionState
from ...tasks import TaskService


def _store(tmp_path, session_id="session"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return AutonomyStore(
        tmp_path / "tasks.sqlite3",
        session_id=session_id,
        workspace_dir=workspace,
    )


def test_once_trigger_materializes_one_durable_run(tmp_path):
    store = _store(tmp_path)
    automation = store.create_automation(
        name="once",
        prompt="do once",
        trigger=TriggerSpec(type="once", run_at=100),
        now=10,
    )

    assert store.materialize_due(now=99) == []
    run_ids = store.materialize_due(now=100)
    assert len(run_ids) == 1
    assert store.materialize_due(now=200) == []
    assert store.get_automation(automation.id).status == "completed"

    claimed = store.claim_next_run(now=100)
    assert claimed is not None
    assert claimed.id == run_ids[0]
    assert claimed.status == "dispatched"
    assert claimed.attempt == 0
    claimed = store.start_run(claimed.id, now=100)
    assert claimed.status == "running"
    assert claimed.attempt == 1
    finished = store.finish_run(claimed.id, status="completed", result="done", now=101)
    assert finished.status == "completed"
    assert finished.result == "done"
    store.close()


def test_definitions_and_pending_external_events_survive_reopen(tmp_path):
    store = _store(tmp_path)
    automation = store.create_automation(
        name="persistent event",
        prompt="handle after restart",
        trigger=TriggerSpec(type="event", event_name="wake"),
        now=0,
    )
    store.emit_event("wake", {"value": 1}, now=1)
    store.close()

    reopened = _store(tmp_path)
    assert reopened.get_automation(automation.id).prompt == "handle after restart"
    run_ids = reopened.materialize_due(now=2)
    assert len(run_ids) == 1
    assert reopened.get_run(run_ids[0]).trigger_payload["payload"] == {"value": 1}
    reopened.close()


def test_interval_coalesces_while_previous_run_is_live(tmp_path):
    store = _store(tmp_path)
    automation = store.create_automation(
        name="interval",
        prompt="repeat",
        trigger=TriggerSpec(type="interval", every_seconds=10, start_at=100),
        now=0,
    )

    first_id = store.materialize_due(now=100)[0]
    first = store.claim_next_run(now=100)
    assert first is not None and first.id == first_id
    store.start_run(first.id, now=100)
    assert store.materialize_due(now=120) == []
    assert store.get_automation(automation.id).next_run_at == 130
    store.finish_run(first_id, status="completed", now=121)
    assert store.materialize_due(now=129) == []
    assert len(store.materialize_due(now=130)) == 1
    store.close()


def test_file_change_and_external_event_triggers(tmp_path):
    store = _store(tmp_path)
    workspace = tmp_path / "workspace"
    file_job = store.create_automation(
        name="watch",
        prompt="inspect change",
        trigger=TriggerSpec(type="file_change", path="watched.txt"),
        now=1,
    )
    event_job = store.create_automation(
        name="webhook",
        prompt="handle event",
        trigger=TriggerSpec(type="event", event_name="deploy.finished"),
        now=1,
    )

    assert store.materialize_due(now=2) == []
    (workspace / "watched.txt").write_text("new", encoding="utf-8")
    file_runs = store.materialize_due(now=3)
    assert len(file_runs) == 1
    assert store.get_run(file_runs[0]).automation_id == file_job.id

    event_id = store.emit_event("deploy.finished", {"sha": "abc"}, now=4)
    event_runs = store.materialize_due(now=5)
    assert len(event_runs) == 1
    event_run = store.get_run(event_runs[0])
    assert event_run.automation_id == event_job.id
    assert event_run.trigger_payload == {
        "event_id": event_id,
        "event_name": "deploy.finished",
        "payload": {"sha": "abc"},
    }
    assert store.materialize_due(now=6) == []
    store.close()


def test_file_change_during_live_run_is_coalesced_not_lost(tmp_path):
    store = _store(tmp_path)
    workspace = tmp_path / "workspace"
    path = workspace / "watched.txt"
    path.write_text("baseline", encoding="utf-8")
    store.create_automation(
        name="watch",
        prompt="inspect",
        trigger=TriggerSpec(type="file_change", path="watched.txt"),
        now=0,
    )
    path.write_text("first", encoding="utf-8")
    first_id = store.materialize_due(now=1)[0]
    path.write_text("second-change", encoding="utf-8")

    assert store.materialize_due(now=2) == []
    store.cancel_run(first_id, "test complete")
    follow_up = store.materialize_due(now=3)
    assert len(follow_up) == 1
    assert follow_up[0] != first_id
    store.close()


def test_web_change_baselines_then_dispatches_only_on_fingerprint_change(tmp_path):
    store = _store(tmp_path)
    automation = store.create_automation(
        name="watch web",
        prompt="summarize page change",
        trigger=TriggerSpec(
            type="web_change",
            url="https://example.com/status",
            every_seconds=10,
        ),
        now=0,
    )

    assert [item.id for item in store.list_due_web_probes(now=0)] == [automation.id]
    assert store.record_web_probe(
        automation.id, {"sha256": "first"}, now=0
    ) is None
    assert store.list_due_web_probes(now=9) == []
    run_id = store.record_web_probe(
        automation.id, {"sha256": "second"}, now=10
    )
    assert run_id is not None
    run = store.get_run(run_id)
    assert run.trigger_payload["before"] == {"sha256": "first"}
    assert run.trigger_payload["after"] == {"sha256": "second"}
    store.close()


def test_scheduler_dispatches_web_change_with_injected_probe(tmp_path):
    store = _store(tmp_path)
    events = queue.Queue()
    fingerprints = iter([
        {"sha256": "first"},
        {"sha256": "second"},
    ])
    store.create_automation(
        name="watch web",
        prompt="handle change",
        trigger=TriggerSpec(
            type="web_change",
            url="https://example.com",
            every_seconds=0.03,
        ),
    )
    scheduler = AutonomyScheduler(
        store,
        events,
        poll_interval=0.01,
        web_probe=lambda url: next(fingerprints),
    )
    scheduler.start()

    event_type, run_id = events.get(timeout=1)
    assert event_type == "DURABLE_RUN_DUE"
    assert store.get_run(str(run_id)).trigger_type == "web_change"
    store.start_run(str(run_id))
    scheduler.finish_run(str(run_id), status="completed")
    scheduler.close()
    store.close()


def test_web_probe_rejects_private_network_targets():
    with pytest.raises(ValueError, match="non-public"):
        probe_public_web_page("http://127.0.0.1/private")


def test_manual_recovery_marks_unknown_but_retry_policy_requeues(tmp_path):
    store = _store(tmp_path)
    manual = store.create_automation(
        name="manual",
        prompt="unsafe work",
        trigger=TriggerSpec(type="once", run_at=0),
        recovery_policy="manual",
        now=0,
    )
    store.materialize_due(now=0)
    manual_run = store.claim_next_run(now=0)
    assert manual_run is not None
    manual_run = store.start_run(manual_run.id, now=0)

    retry = store.create_automation(
        name="retry",
        prompt="idempotent work",
        trigger=TriggerSpec(type="once", run_at=1),
        recovery_policy="retry",
        max_retries=1,
        retry_delay_seconds=5,
        now=0,
    )
    store.materialize_due(now=1)
    # manual_run still occupies no scheduler slot at the store layer; claiming
    # directly is allowed and useful for recovery-state tests.
    retry_run = store.claim_next_run(now=1)
    assert retry_run is not None and retry_run.automation_id == retry.id
    retry_run = store.start_run(retry_run.id, now=1)

    recovered = {run.id: run for run in store.recover_interrupted(now=10)}
    assert recovered[manual_run.id].status == "unknown"
    assert "not replayed" in recovered[manual_run.id].error
    assert recovered[retry_run.id].status == "waiting_retry"
    assert store.claim_next_run(now=14) is None
    retried = store.claim_next_run(now=15)
    assert retried is not None and retried.id == retry_run.id
    retried = store.start_run(retried.id, now=15)
    assert retried.attempt == 2
    store.close()


def test_dispatched_but_not_started_run_is_safely_requeued(tmp_path):
    store = _store(tmp_path)
    store.create_automation(
        name="dispatch",
        prompt="not started yet",
        trigger=TriggerSpec(type="once", run_at=0),
        recovery_policy="manual",
        now=0,
    )
    store.materialize_due(now=0)
    dispatched = store.claim_next_run(now=0)
    assert dispatched is not None and dispatched.status == "dispatched"

    recovered = store.recover_interrupted(now=1)

    assert recovered[0].id == dispatched.id
    assert recovered[0].status == "queued"
    assert recovered[0].attempt == 0
    store.close()


def test_checkpoint_owned_running_run_is_protected_from_store_recovery(tmp_path):
    store = _store(tmp_path)
    store.create_automation(
        name="resume",
        prompt="continue transcript",
        trigger=TriggerSpec(type="once", run_at=0),
        recovery_policy="manual",
        now=0,
    )
    store.materialize_due(now=0)
    dispatched = store.claim_next_run(now=0)
    assert dispatched is not None
    running = store.start_run(dispatched.id, now=0)

    assert store.recover_interrupted(
        active_run_ids=[running.id], now=1
    ) == []
    assert store.get_run(running.id).status == "running"
    store.close()


def test_failed_run_retries_only_within_explicit_budget(tmp_path):
    store = _store(tmp_path)
    store.create_automation(
        name="retry failure",
        prompt="idempotent",
        trigger=TriggerSpec(type="once", run_at=0),
        recovery_policy="retry",
        max_retries=1,
        retry_delay_seconds=5,
        now=0,
    )
    store.materialize_due(now=0)
    first = store.claim_next_run(now=0)
    assert first is not None
    first = store.start_run(first.id, now=0)
    waiting = store.finish_run(first.id, status="failed", error="first", now=1)
    assert waiting.status == "waiting_retry"
    assert store.claim_next_run(now=5) is None
    second = store.claim_next_run(now=6)
    assert second is not None
    second = store.start_run(second.id, now=6)
    failed = store.finish_run(second.id, status="failed", error="second", now=7)
    assert failed.status == "failed"
    assert failed.attempt == 2
    assert failed.error == "second"
    store.close()


def test_task_service_projects_and_cancels_durable_runs(tmp_path):
    store = _store(tmp_path)
    session = SessionState.create("root", tmp_path / "workspace")
    session.durable_task_store = store
    automation = store.create_automation(
        name="pending",
        prompt="later",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    run_id = store.materialize_due(now=0)[0]
    service = TaskService.for_session(session)

    task = service.get(run_id)
    assert task.kind == "durable"
    assert task.status == "pending"
    assert task.details["automation_id"] == automation.id
    cancelled = service.cancel(run_id, reason="not needed")
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_reason == "not needed"
    store.close()


def test_scheduler_dispatches_one_run_and_stops_until_finished(tmp_path):
    store = _store(tmp_path)
    events = queue.Queue()
    first = store.create_automation(
        name="first",
        prompt="first",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    second = store.create_automation(
        name="second",
        prompt="second",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    scheduler = AutonomyScheduler(store, events, poll_interval=0.01)
    scheduler.start()

    event_type, first_run_id = events.get(timeout=1)
    assert event_type == "DURABLE_RUN_DUE"
    assert events.empty()
    store.start_run(str(first_run_id))
    scheduler.finish_run(str(first_run_id), status="completed", result="ok")
    event_type, second_run_id = events.get(timeout=1)
    assert event_type == "DURABLE_RUN_DUE"
    assert first_run_id != second_run_id
    store.start_run(str(second_run_id))
    assert {
        store.get_run(str(first_run_id)).automation_id,
        store.get_run(str(second_run_id)).automation_id,
    } == {first.id, second.id}
    scheduler.finish_run(str(second_run_id), status="completed", result="ok")
    scheduler.close()
    store.close()


def test_cancelled_dispatched_run_does_not_block_later_dispatch(tmp_path):
    store = _store(tmp_path)
    events = queue.Queue()
    store.create_automation(
        name="first",
        prompt="first",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    store.create_automation(
        name="second",
        prompt="second",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    scheduler = AutonomyScheduler(store, events, poll_interval=0.01)
    scheduler.start()

    event_type, first_run_id = events.get(timeout=1)
    assert event_type == "DURABLE_RUN_DUE"
    assert store.get_run(str(first_run_id)).status == "dispatched"
    store.cancel_run(str(first_run_id), "no longer needed")
    scheduler.notify_changed()

    event_type, second_run_id = events.get(timeout=1)
    assert event_type == "DURABLE_RUN_DUE"
    assert str(second_run_id) != str(first_run_id)
    assert store.get_run(str(first_run_id)).status == "cancelled"
    store.start_run(str(second_run_id))
    scheduler.finish_run(str(second_run_id), status="completed", result="ok")
    scheduler.close()
    store.close()


def test_finish_run_exception_releases_dispatch_gate(tmp_path):
    store = _store(tmp_path)
    events = queue.Queue()
    store.create_automation(
        name="first",
        prompt="first",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    store.create_automation(
        name="second",
        prompt="second",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    scheduler = AutonomyScheduler(store, events, poll_interval=0.01)
    scheduler.start()

    _, first_run_id = events.get(timeout=1)
    store.start_run(str(first_run_id))
    real_finish = store.finish_run

    def finish_then_fail(run_id, **kwargs):
        real_finish(run_id, **kwargs)
        raise AutonomyStoreError("simulated post-commit failure")

    store.finish_run = finish_then_fail
    with pytest.raises(AutonomyStoreError, match="post-commit"):
        scheduler.finish_run(str(first_run_id), status="completed")

    _, second_run_id = events.get(timeout=1)
    assert str(second_run_id) != str(first_run_id)
    assert store.get_run(str(first_run_id)).status == "completed"
    store.start_run(str(second_run_id))
    store.finish_run = real_finish
    scheduler.finish_run(str(second_run_id), status="completed")
    scheduler.close()
    store.close()


def test_event_trigger_coalesces_while_live_and_replays_after(tmp_path):
    store = _store(tmp_path)
    job = store.create_automation(
        name="webhook",
        prompt="handle",
        trigger=TriggerSpec(type="event", event_name="deploy.finished"),
        now=0,
    )
    store.emit_event("deploy.finished", {"n": 1}, now=1)
    first_ids = store.materialize_due(now=2)
    assert len(first_ids) == 1
    store.emit_event("deploy.finished", {"n": 2}, now=3)
    store.emit_event("deploy.finished", {"n": 3}, now=4)
    assert store.materialize_due(now=5) == []
    queued = [
        run for run in store.list_runs(job.id) if run.status == "queued"
    ]
    assert len(queued) == 1

    first = store.claim_next_run(now=5)
    assert first is not None
    store.start_run(first.id, now=5)
    store.finish_run(first.id, status="completed", now=6)

    follow = store.materialize_due(now=7)
    assert len(follow) == 1
    payload = store.get_run(follow[0]).trigger_payload
    assert payload["payload"] == {"n": 3}
    assert payload["coalesced_count"] == 2
    assert store.materialize_due(now=8) == []
    store.close()


def test_interval_next_run_at_does_not_drift_with_late_polls(tmp_path):
    store = _store(tmp_path)
    automation = store.create_automation(
        name="interval",
        prompt="repeat",
        trigger=TriggerSpec(type="interval", every_seconds=10, start_at=100),
        now=0,
    )
    created = store.materialize_due(now=100.4)
    assert len(created) == 1
    assert store.get_automation(automation.id).next_run_at == 110
    claimed = store.claim_next_run(now=100.4)
    assert claimed is not None
    store.start_run(claimed.id, now=100.4)
    store.finish_run(claimed.id, status="completed", now=100.5)

    store.materialize_due(now=110.4)
    assert store.get_automation(automation.id).next_run_at == 120
    claimed = store.claim_next_run(now=110.4)
    assert claimed is not None
    store.start_run(claimed.id, now=110.4)
    store.finish_run(claimed.id, status="completed", now=110.5)

    store.materialize_due(now=120.4)
    assert store.get_automation(automation.id).next_run_at == 130
    store.close()


def test_interval_skipped_ticks_do_not_update_last_run_at(tmp_path):
    store = _store(tmp_path)
    automation = store.create_automation(
        name="interval",
        prompt="repeat",
        trigger=TriggerSpec(type="interval", every_seconds=10, start_at=100),
        now=0,
    )
    store.materialize_due(now=100)
    assert store.get_automation(automation.id).last_run_at == 100
    claimed = store.claim_next_run(now=100)
    assert claimed is not None
    store.start_run(claimed.id, now=100)

    assert store.materialize_due(now=120) == []
    skipped = store.get_automation(automation.id)
    assert skipped.next_run_at == 130
    assert skipped.last_run_at == 100
    store.close()


def test_hanging_web_probe_does_not_block_other_dispatch(tmp_path):
    store = _store(tmp_path)
    events = queue.Queue()
    released = threading.Event()

    def hanging_probe(url):
        released.wait(timeout=5)
        return {"sha256": "hung"}

    store.create_automation(
        name="watch web",
        prompt="hang",
        trigger=TriggerSpec(
            type="web_change",
            url="https://example.com",
            every_seconds=60,
        ),
        now=0,
    )
    store.create_automation(
        name="once",
        prompt="run now",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    scheduler = AutonomyScheduler(
        store,
        events,
        poll_interval=0.01,
        web_probe=hanging_probe,
    )
    scheduler.start()
    try:
        event_type, run_id = events.get(timeout=1)
        assert event_type == "DURABLE_RUN_DUE"
        assert store.get_run(str(run_id)).trigger_type == "once"
    finally:
        released.set()
        scheduler.close()
        store.close()
