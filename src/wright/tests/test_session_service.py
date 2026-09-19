import queue
import threading
import time
from types import SimpleNamespace

import pytest

from wright.autonomy import AutonomyStore
from wright.interaction import InteractionBroker
from wright.session_service import (
    SessionClosedError,
    SessionService,
    SessionServiceError,
)
from wright.ui_events import EventPublisher


def _runtime(*, status="completed", model="first"):
    publisher = EventPublisher(project_id="project", session_id="session")
    idle = threading.Event()
    idle.set()
    state = SimpleNamespace(
        session_id="session",
        status=status,
        model_name=model,
        user_goal="test",
        environment="local",
        workspace_dir=".",
        project_root=".",
        base_commit=None,
        branch_name=None,
        llm_transport="chat",
        lifecycle="open",
        attachment_records=lambda ids: [],
    )
    broker = InteractionBroker(publisher)
    llm = SimpleNamespace(model=model)
    return SimpleNamespace(
        session_state=state,
        publisher=publisher,
        interaction_broker=broker,
        event_queue=queue.Queue(),
        agent_idle=idle,
        cancellation_event=threading.Event(),
        llm=llm,
        agent=SimpleNamespace(llm=llm, checkpoint_store=None, continue_run=lambda: None),
        resumed=False,
    )


def _durable_runtime(tmp_path):
    runtime = _runtime()
    runtime.autonomy_store = AutonomyStore(
        tmp_path / "autonomy.sqlite", session_id="session", workspace_dir=tmp_path
    )
    return runtime


def test_service_deduplicates_and_cancels_only_the_selected_queued_input():
    runtime = _runtime()
    seen = []
    first_started = threading.Event()
    release = threading.Event()
    cleaned = []

    def consume(_runtime, event_type, payload):
        if event_type == "EXIT":
            return True
        seen.append(payload["prompt"])
        if payload["prompt"] == "first":
            first_started.set()
            release.wait(1)
        return False

    service = SessionService(runtime, event_processor=consume, shutdown=lambda _: cleaned.append(True))
    service.start()
    assert service.submit("first", "one")["payload"]["duplicate"] is False
    assert first_started.wait(1)
    service.submit("second", "two")
    service.submit("third", "three")
    assert service.submit("first", "one")["payload"]["duplicate"] is True
    service.cancel_queued("cancel-two", "two")
    assert [item["command_id"] for item in service.snapshot()["queued_commands"]] == ["three"]
    release.set()
    deadline = time.monotonic() + 1
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["first", "third"]
    assert service.close(wait_timeout=1)
    assert cleaned == [True]


def test_interaction_response_and_late_duplicate_have_explicit_results():
    runtime = _runtime()
    service = SessionService(runtime, shutdown=lambda _: None)
    answer = []
    waiting = threading.Thread(
        target=lambda: answer.append(runtime.interaction_broker.request("ask_user", {"question": "q"}))
    )
    waiting.start()
    deadline = time.monotonic() + 1
    while not runtime.interaction_broker.snapshot() and time.monotonic() < deadline:
        time.sleep(0.01)
    request_id = runtime.interaction_broker.snapshot()[0]["request_id"]
    accepted = service.respond_interaction("reply", request_id, "yes")
    repeated = service.respond_interaction("reply", request_id, "yes")
    waiting.join(1)
    assert accepted["type"] == "command.accepted"
    assert repeated["payload"]["duplicate"] is True
    assert answer == ["yes"]


def test_close_waits_for_worker_before_releasing_dependencies_and_is_idempotent():
    runtime = _runtime()
    entered = threading.Event()
    release = threading.Event()
    cleaned = []

    def consume(_runtime, event_type, _payload):
        if event_type == "EXIT":
            return True
        entered.set()
        release.wait(1)
        return False

    service = SessionService(runtime, event_processor=consume, shutdown=lambda _: cleaned.append(True))
    service.start()
    service.submit("work", "work")
    assert entered.wait(1)
    assert service.close(wait_timeout=0.01) is False
    assert service.runner.state == "closing"
    assert cleaned == []
    with pytest.raises(SessionClosedError):
        service.submit("too late", "late")
    release.set()
    assert service.runner.join(1)
    assert service.runner.state == "closed"
    assert cleaned == [True]
    assert service.close(wait_timeout=0) is True
    assert cleaned == [True]


def test_model_persistence_rolls_back_on_failure_and_running_turn_is_rejected():
    runtime = _runtime()

    class BrokenStore:
        def save(self, _state):
            raise OSError("disk full")

    runtime.agent.checkpoint_store = BrokenStore()
    service = SessionService(runtime, shutdown=lambda _: None)
    with pytest.raises(SessionServiceError, match="checkpoint failed: disk full"):
        service.set_model("second")
    assert runtime.llm.model == "first"
    assert runtime.session_state.model_name == "first"
    runtime.agent_idle.clear()
    with pytest.raises(SessionServiceError, match="while turn is running"):
        service.set_model("third")


def test_completed_resume_never_continues_a_completed_task():
    runtime = _runtime(status="completed")
    runtime.resumed = True
    continued = []
    runtime.agent.continue_run = lambda: continued.append(True)
    service = SessionService(runtime, shutdown=lambda _: None)
    service.start()
    assert service.close(wait_timeout=1)
    assert continued == []


def test_durably_accepted_input_is_recovered_once_by_real_service_entry(tmp_path):
    runtime = _durable_runtime(tmp_path)
    store = runtime.autonomy_store
    store.accept_command(
        "session:session", "lost-between-accept-and-queue",
        {"command": "turn.submit", "prompt": "recover me", "attachment_ids": []},
    )
    seen = []

    def consume(_runtime, event_type, payload):
        if event_type == "EXIT":
            return True
        seen.append(payload["prompt"])
        return False

    service = SessionService(runtime, event_processor=consume, shutdown=lambda _: None)
    service.start()
    deadline = time.monotonic() + 1
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["recover me"]
    record = service.command_status("lost-between-accept-and-queue")
    assert record["status"] == "completed"
    assert record["result"]["run_id"] == ""
    assert service.close(wait_timeout=1)
    store.close()


def test_same_durable_id_requeues_pending_but_rejects_changed_payload(tmp_path):
    runtime = _durable_runtime(tmp_path)
    seen = []

    def consume(_runtime, event_type, payload):
        if event_type == "EXIT":
            return True
        seen.append(payload["prompt"])
        return False

    service = SessionService(runtime, event_processor=consume, shutdown=lambda _: None)
    first = service.submit("once", "stable")
    duplicate = service.submit("once", "stable")
    assert first["payload"]["duplicate"] is False
    assert duplicate["payload"]["duplicate"] is True
    with pytest.raises(SessionServiceError, match="different content"):
        service.submit("changed", "stable")
    service.start()
    deadline = time.monotonic() + 1
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["once"]
    assert service.close(wait_timeout=1)
    runtime.autonomy_store.close()


def test_two_session_consumers_claim_one_persisted_command_once(tmp_path):
    first = _durable_runtime(tmp_path)
    second = _runtime()
    second.autonomy_store = AutonomyStore(
        tmp_path / "autonomy.sqlite", session_id="session", workspace_dir=tmp_path
    )
    first.autonomy_store.accept_command(
        "session:session", "racing-command",
        {"command": "turn.submit", "prompt": "only once", "attachment_ids": []},
    )
    seen: list[str] = []
    seen_lock = threading.Lock()

    def consume(_runtime, event_type, payload):
        if event_type == "EXIT":
            return True
        with seen_lock:
            seen.append(payload["prompt"])
        return False

    left = SessionService(first, event_processor=consume, shutdown=lambda _: None)
    right = SessionService(second, event_processor=consume, shutdown=lambda _: None)
    left.start()
    right.start()
    deadline = time.monotonic() + 1
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["only once"]
    assert first.autonomy_store.get_command("session:session", "racing-command")["status"] == "completed"
    assert left.close(wait_timeout=1)
    assert right.close(wait_timeout=1)
    first.autonomy_store.close()
    second.autonomy_store.close()


def test_running_command_persists_run_identity_before_processor_returns(tmp_path):
    runtime = _durable_runtime(tmp_path)

    def consume(current, event_type, _payload):
        if event_type == "EXIT":
            return True
        current.agent.on_run_started("run_before_effect")
        record = current.autonomy_store.get_command("session:session", "cmd")
        assert record["status"] == "running"
        assert record["run_id"] == "run_before_effect"
        return False

    service = SessionService(runtime, event_processor=consume, shutdown=lambda _: None)
    service.start()
    service.submit("run", "cmd")
    deadline = time.monotonic() + 1
    while service.command_status("cmd")["status"] != "completed" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.command_status("cmd")["run_id"] == "run_before_effect"
    assert service.close(wait_timeout=1)
    runtime.autonomy_store.close()
