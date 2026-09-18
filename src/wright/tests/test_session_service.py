import queue
import threading
import time
from types import SimpleNamespace

import pytest

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
