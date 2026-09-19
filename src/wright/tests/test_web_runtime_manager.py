import argparse
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from ..interaction import InteractionBroker
from ..processes import RuntimeResources
from ..project import ProjectContext
from ..session import Session
from ..tools.base import ArtifactRef, ToolCall, ToolResult
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
            context_tokens=0,
            task_usage=lambda: SimpleNamespace(
                prompt_tokens=0, completion_tokens=0, total_tokens=0
            ),
            turns=[],
            plan_manager=SimpleNamespace(snapshot=lambda: {"steps": []}),
        ),
        publisher=publisher,
        interaction_broker=InteractionBroker(publisher),
        event_queue=queue.Queue(),
        agent_idle=threading.Event(),
        cancellation_event=threading.Event(),
        llm=SimpleNamespace(context_limit=128_000),
        runtime_resources=RuntimeResources(session_id),
        project_context=ProjectContext(
            project_root=root,
            execution_root=root,
            environment="worktree",
            base_commit="abc",
            branch_name=f"wright/{session_id}",
        ),
    )


def test_snapshot_uses_live_response_projection_after_event_ring_eviction(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    runtime = _fake_runtime("live-snapshot", tmp_path)
    session = Session.create("active goal", tmp_path, session_id="live-snapshot")
    session.begin_user_turn("active goal")
    runtime.session_state = session
    runtime.publisher = EventPublisher(
        project_id="project", session_id=session.session_id, max_events=1
    )
    runtime.interaction_broker = InteractionBroker(runtime.publisher)
    resources = RuntimeResources.for_session(session.session_id)
    assert resources is not None
    runtime.runtime_resources = resources
    run = session.active_run()
    assert run is not None
    resources.begin_response(run.run_id)
    resources.append_reasoning("reasoning survives")
    resources.append_content("complete streamed response")
    resources.update_tool("call_1", {"call_id": "call_1", "name": "read_file"})
    for number in range(3):
        runtime.publisher.publish("content.delta", {"piece": str(number)})

    handle = SessionHandle(runtime)
    snapshot = handle.snapshot()

    assert snapshot["active_turn"] == {
        "run_id": run.run_id,
        "turn_id": None,
        "prompt": "active goal",
        "attachments": [],
        "reasoning": "reasoning survives",
        "content": "complete streamed response",
        "tools": [{"call_id": "call_1", "name": "read_file"}],
    }
    assert len(runtime.publisher.retained_events()) == 1
    handle.close()


def test_history_projects_run_owned_artifacts_after_event_cache_eviction(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    runtime = _fake_runtime("artifact-history", tmp_path)
    session = Session.create("goal", tmp_path, session_id="artifact-history")
    session.begin_user_turn("deliver a report")
    session.append_message({"role": "user", "content": "deliver a report"})
    session.record_assistant_turn(
        "", {}, "tool_calls", [ToolCall("make_report", {}, "report-call")],
    )
    session.record_tool_execution(
        "report-call",
        ToolResult.success(
            {"message": "created"},
            artifacts=(ArtifactRef(
                "artifact-report", "text/markdown", "report.md", 12,
                "", "report-call", "artifact-report",
            ),),
        ),
    )
    session.record_assistant_turn(
        "report delivered", {"final_answer": "report delivered"}, "final",
    )
    run = session.active_run()
    assert run is not None
    run.finish("completed", result="report delivered")
    runtime.session_state = session

    handle = SessionHandle(runtime)
    history = handle.snapshot()["history"]

    assert history[0]["tools"][0]["artifacts"] == [{
        "id": "artifact-report", "media_type": "text/markdown",
        "name": "report.md", "size": 12, "run_id": "",
        "call_id": "report-call", "storage_path": "artifact-report",
    }]
    handle.close()


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


def test_cancel_rejects_queued_turn_before_it_starts(monkeypatch, tmp_path):
    first_started = threading.Event()
    release_first = threading.Event()
    seen: list[str] = []

    def process(rt, event_type, payload):
        if event_type == "EXIT":
            return True
        seen.append(payload["prompt"])
        if len(seen) == 1:
            first_started.set()
            release_first.wait(timeout=2)
        return False

    monkeypatch.setattr(runtime_module, "process_session_event", process)
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    handle = SessionHandle(_fake_runtime("cancel-queue", tmp_path))
    handle.submit("first", "command-one")
    assert first_started.wait(timeout=1)
    handle.submit("second", "command-two")
    cancelled = handle.cancel("cancel-command")
    assert cancelled["payload"]["cancelled_queued"] == 1
    release_first.set()

    deadline = time.monotonic() + 1
    while not any(
        event.type == "command.rejected" and event.payload.get("command_id") == "command-two"
        for event in handle.publisher.retained_events()
    ) and time.monotonic() < deadline:
        time.sleep(0.005)

    assert seen == ["first"]
    handle.close()


def test_cancel_queued_removes_only_target_instruction(monkeypatch, tmp_path):
    first_started = threading.Event()
    release_first = threading.Event()
    seen: list[str] = []

    def process(rt, event_type, payload):
        if event_type == "EXIT":
            return True
        seen.append(payload["prompt"])
        if payload["prompt"] == "first":
            first_started.set()
            release_first.wait(timeout=2)
        return False

    monkeypatch.setattr(runtime_module, "process_session_event", process)
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    handle = SessionHandle(_fake_runtime("cancel-one", tmp_path))
    handle.submit("first", "command-one")
    assert first_started.wait(timeout=1)
    handle.submit("second", "command-two")
    handle.submit("third", "command-three")

    cancelled = handle.cancel_queued("cancel-second", "command-two")
    assert cancelled["type"] == "command.accepted"
    assert handle.snapshot()["queued_commands"] == [
        {"command_id": "command-three", "prompt": "third"}
    ]
    release_first.set()

    deadline = time.monotonic() + 1
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert seen == ["first", "third"]
    handle.close()


def _handle_with_model(monkeypatch, tmp_path, *, model="first", idle=True, store=None):
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    runtime = _fake_runtime("model-session", tmp_path)
    llm = SimpleNamespace(model=model)
    runtime.llm = llm
    runtime.agent = SimpleNamespace(llm=llm, checkpoint_store=store)
    runtime.session_state.model_name = model
    if idle:
        runtime.agent_idle.set()
    else:
        runtime.agent_idle.clear()
    return SessionHandle(runtime), llm


def test_set_model_updates_llm_and_session(monkeypatch, tmp_path):
    handle, llm = _handle_with_model(monkeypatch, tmp_path)
    summary = handle.set_model("second")
    assert llm.model == "second"
    assert handle.runtime.session_state.model_name == "second"
    assert summary["model"] == "second"
    handle.close()


def test_set_model_rejects_empty_and_running_turn(monkeypatch, tmp_path):
    idle, _llm = _handle_with_model(monkeypatch, tmp_path)
    with pytest.raises(RuntimeManagerError, match="cannot be empty"):
        idle.set_model("  ")
    idle.close()

    running, llm = _handle_with_model(monkeypatch, tmp_path, idle=False)
    with pytest.raises(RuntimeManagerError, match="while turn is running"):
        running.set_model("second")
    assert llm.model == "first"
    running.close()


def test_set_model_rolls_back_on_checkpoint_failure(monkeypatch, tmp_path):
    class BoomStore:
        def save(self, _state):
            raise RuntimeError("disk full")

    handle, llm = _handle_with_model(monkeypatch, tmp_path, store=BoomStore())
    with pytest.raises(RuntimeManagerError, match="checkpoint failed: disk full"):
        handle.set_model("second")
    assert llm.model == "first"
    assert handle.runtime.session_state.model_name == "first"
    handle.close()


def test_manager_set_model_missing_session_is_404(tmp_path):
    manager = RuntimeManager(tmp_path, capacity=1, base_args=argparse.Namespace())
    with pytest.raises(RuntimeManagerError, match="not found") as exc_info:
        manager.set_model("missing", "gpt-4o")
    assert exc_info.value.status_code == 404


def test_project_exposes_configured_models_not_a_hardcoded_default(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("WRIGHT_MODELS", raising=False)
    manager = RuntimeManager(tmp_path, capacity=1, base_args=argparse.Namespace(model=None))

    empty = manager.project()
    assert empty["default_model"] == ""
    assert empty["models"] == []

    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("WRIGHT_MODELS", "deepseek-v4-flash,deepseek-chat")
    configured = manager.project()
    assert configured["default_model"] == "deepseek-v4-flash"
    assert configured["models"] == ["deepseek-v4-flash", "deepseek-chat"]

    cli_manager = RuntimeManager(
        tmp_path, capacity=1, base_args=argparse.Namespace(model="cli-model")
    )
    overridden = cli_manager.project()
    assert overridden["default_model"] == "cli-model"
    assert overridden["models"] == ["cli-model", "deepseek-v4-flash", "deepseek-chat"]


def test_closed_local_session_can_be_replaced_without_losing_its_schedules(monkeypatch, tmp_path):
    from .. import runtime as assembly
    from ..autonomy import TriggerSpec
    from .responses import response

    class Model:
        model = "offline"
        transport_name = "chat"
        context_limit = 128_000

        def __call__(self, *_args, **_kwargs):
            yield response(content="done")

    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_MODEL", "offline")
    monkeypatch.setattr(assembly, "load_env", lambda: None)
    monkeypatch.setattr(assembly, "LLMClient", lambda **_: Model())
    monkeypatch.setattr(assembly, "load_mcp_configs", lambda _: [])
    monkeypatch.setattr(assembly, "optional_knowledge_tools", list)
    monkeypatch.setattr(assembly, "load_lifecycle_manager", lambda *a, **k: SimpleNamespace(emit=lambda *a, **k: None))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = RuntimeManager(workspace, base_args=argparse.Namespace())
    try:
        first = manager.create(environment="local")
        first_id = first.runtime.session_state.session_id
        host = first.runtime.application_host
        first_store = first.runtime.autonomy_store
        auto = first_store.create_automation(
            name="retained", prompt="work", trigger=TriggerSpec(type="once", run_at=time.time() + 3600),
        )
        manager.close(first_id)
        second = manager.create(environment="local")
        second_id = second.runtime.session_state.session_id
        assert second.runtime.application_host is host
        assert second.runtime.autonomy_store.session_id == second_id
        assert second.runtime.autonomy_store.list_automations() == []
        assert first_store.get_automation(auto.id).session_id == first_id
        second_store = second.runtime.autonomy_store
        manager.close(second_id)
        restored = manager.create(resume_session_id=first_id)
        assert restored.runtime.application_host is host
        assert restored.runtime.autonomy_store is first_store
        assert restored.runtime.autonomy_store.list_automations()[0].id == auto.id
        assert len(manager._application_hosts) == 1
    finally:
        manager.shutdown()
    assert first_store.closed and second_store.closed
