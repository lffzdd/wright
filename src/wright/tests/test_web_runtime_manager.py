import argparse
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from ..interaction import InteractionBroker
from ..project import ProjectContext
from ..ui_events import EventPublisher
from ..web import runtime_manager as runtime_module
from ..web.runtime_manager import RuntimeManager, RuntimeManagerError, SessionHandle


def _fake_runtime(session_id, root):
    publisher = EventPublisher(project_id="project", session_id=session_id)
    return SimpleNamespace(
        session_state=SimpleNamespace(
            session_id=session_id,
            status="running",
            user_goal="test",
            model_name="model",
            environment="worktree",
            workspace_dir=root,
            project_root=root,
            base_commit="abc",
            branch_name=f"wright/{session_id}",
        ),
        publisher=publisher,
        interaction_broker=InteractionBroker(publisher),
        event_queue=queue.Queue(),
        agent_idle=threading.Event(),
        cancellation_event=threading.Event(),
        project_context=ProjectContext(
            project_root=root,
            execution_root=root,
            environment="worktree",
            base_commit="abc",
            branch_name=f"wright/{session_id}",
        ),
    )


def test_separate_session_workers_execute_in_parallel(monkeypatch, tmp_path):
    barrier = threading.Barrier(2)
    completed = threading.Event()
    lock = threading.Lock()
    seen = []

    def process(rt, event_type, payload):
        if event_type == "EXIT":
            return True
        barrier.wait(timeout=1)
        with lock:
            seen.append(rt.session_state.session_id)
            if len(seen) == 2:
                completed.set()
        rt.agent_idle.set()
        return False

    monkeypatch.setattr(runtime_module, "process_session_event", process)
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    first = SessionHandle(_fake_runtime("first", tmp_path))
    second = SessionHandle(_fake_runtime("second", tmp_path))

    first.submit("one", "command-one")
    second.submit("two", "command-two")

    assert completed.wait(timeout=2)
    assert set(seen) == {"first", "second"}
    first.close()
    second.close()


def test_one_session_processes_submitted_turns_fifo(monkeypatch, tmp_path):
    seen = []
    done = threading.Event()

    def process(rt, event_type, payload):
        if event_type == "EXIT":
            return True
        seen.append(payload["prompt"])
        if len(seen) == 2:
            done.set()
        return False

    monkeypatch.setattr(runtime_module, "process_session_event", process)
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    handle = SessionHandle(_fake_runtime("fifo", tmp_path))

    handle.submit("first", "command-one")
    handle.submit("second", "command-two")

    assert done.wait(timeout=2)
    assert seen == ["first", "second"]
    handle.close()


def test_manager_rejects_duplicate_restore_local_conflict_and_capacity(tmp_path):
    args = argparse.Namespace()
    manager = RuntimeManager(tmp_path, capacity=1, base_args=args)
    existing = SimpleNamespace(
        runtime=SimpleNamespace(project_context=ProjectContext.local(tmp_path))
    )
    manager._handles["active"] = existing

    with pytest.raises(RuntimeManagerError, match="capacity"):
        manager.create(environment="local")
    manager.capacity = 2
    with pytest.raises(RuntimeManagerError, match="already active"):
        manager.create(resume_session_id="active")
    with pytest.raises(RuntimeManagerError, match="local checkout"):
        manager.create(environment="local")


def test_duplicate_command_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    handle = SessionHandle(_fake_runtime("dedupe", tmp_path))

    first = handle.submit("run", "same-command")
    second = handle.submit("run", "same-command")

    assert first["payload"]["duplicate"] is False
    assert second["payload"]["duplicate"] is True
    deadline = time.monotonic() + 1
    while handle.runtime.event_queue.qsize() and time.monotonic() < deadline:
        time.sleep(0.005)
    handle.close()
