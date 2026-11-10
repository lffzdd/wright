import argparse
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from wright.application.session_runtime.live_resources import RuntimeResources
from wright.application.session_runtime.publisher import EventPublisher
from wright.domain.model.session import Session, SessionAuthorization
from wright.domain.model.tool import ArtifactRef, ToolCall, ToolResult
from wright.domain.model.workspace.project import ProjectContext
from wright.interfaces.interaction import InteractionBroker
from wright.interfaces.web import runtime_manager as runtime_module
from wright.interfaces.web.runtime_manager import (
    RuntimeManager,
    RuntimeManagerError,
    SessionHandle,
)


def _fake_runtime(session_id, root):
    publisher = EventPublisher(project_id="project", session_id=session_id)
    return SimpleNamespace(
        session_state=SimpleNamespace(
            agent_task_id=None,
            session_id=session_id,
            authorization=SessionAuthorization(session_id),
            status="running",
            user_goal="test",
            model_name="model",
            environment="worktree",
            execution_mode="sandbox",
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
    resources = RuntimeResources(session.session_id)
    runtime.runtime_resources = resources
    run = session.active_run()
    assert run is not None
    runtime.publisher.publish(
        "turn.started", {"prompt": "active goal", "run_id": run.run_id, "message_index": 0}, turn_id="turn-1",
    )
    runtime.publisher.publish("reasoning.delta", {"piece": "reasoning survives"})
    runtime.publisher.publish("content.delta", {"piece": "complete streamed response"})
    runtime.publisher.publish("tool.planned", {"call_id": "call_1", "name": "read_file"})
    for number in range(3):
        runtime.publisher.publish("system.notice", {"text": str(number)})

    handle = SessionHandle(runtime)
    snapshot = handle.snapshot()

    assert snapshot["active_turn"]["run_id"] == run.run_id
    assert snapshot["active_turn"]["prompt"] == "active goal"
    assert snapshot["active_turn"]["reasoning"] == ""
    assert [item.get("text") for item in snapshot["timeline"][:3]] == [
        "active goal", "reasoning survives", "complete streamed response",
    ]
    assert snapshot["active_turn"]["content"] == ""
    assert snapshot["active_turn"]["segment"] == 1
    assert snapshot["active_turn"]["tools"][0]["call_id"] == "call_1"
    assert snapshot["active_turn"]["tools"][0]["name"] == "read_file"
    assert runtime.publisher.replay(runtime.publisher.stream_id, 0) is None
    assert snapshot["last_seq"] == runtime.publisher.latest_seq
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
        "call_id": "report-call",
    }]
    assert "storage_path" not in history[0]["tools"][0]["artifacts"][0]
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


def test_manager_rejects_capacity_and_duplicate_resume_without_blocking_idle_local(tmp_path):
    args = argparse.Namespace()
    manager = RuntimeManager(tmp_path, capacity=1, base_args=args)
    manager.directory._sessions["active"] = SimpleNamespace()

    with pytest.raises(RuntimeManagerError, match="busy"):
        manager.create(environment="local")
    manager.directory.capacity = 2
    with pytest.raises(RuntimeManagerError, match="already active"):
        manager.create(resume="active")


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
    kept = handle.cancel("stop-current")
    assert kept["payload"]["cancelled_queued"] == 0
    cancelled = handle.cancel_all("cancel-command")
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
    changed = [event for event in handle.publisher.retained_events() if event.type == "session.status_changed"]
    assert changed[-1].payload == {"session_id": handle.session_id, "model": "second"}
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
    from tests.responses import response
    from wright.application.composition import runtime as assembly
    from wright.domain.model.scheduling import TriggerSpec

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
        auto = first_store.create_job(
            name="retained", prompt="work", trigger=TriggerSpec(type="once", run_at=time.time() + 3600),
        )
        manager.close(first_id)
        second = manager.create(environment="local")
        second_id = second.runtime.session_state.session_id
        assert second.runtime.application_host is host
        assert second.runtime.autonomy_store.session_id == second_id
        assert second.runtime.autonomy_store.list_jobs().records == ()
        assert first_store.get_job(auto.id).session_id == first_id
        second_store = second.runtime.autonomy_store
        manager.close(second_id)
        restored = manager.create(resume=first_id)
        assert restored.runtime.application_host is host
        assert restored.runtime.autonomy_store is first_store
        assert restored.runtime.autonomy_store.list_jobs().records[0].id == auto.id
        assert len(manager.directory._hosts) == 1
    finally:
        manager.shutdown()
    assert first_store.closed and second_store.closed


def test_reasoning_snapshot_preserves_order_without_duplicating_committed_steps(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "shutdown_runtime", lambda runtime: None)
    runtime = _fake_runtime("reasoning-order", tmp_path)
    session = Session.create("repeat", tmp_path, session_id="reasoning-order")
    session.begin_user_turn("repeat")
    session.append_user_message("repeat")
    session.record_assistant_turn("Earlier answer", {}, "final")
    session.mark_completed()
    runtime.session_state = session
    publisher = runtime.publisher
    start = len(session.message_records)
    publisher.publish("turn.started", {"prompt": "repeat", "message_index": start}, turn_id="turn-2")
    session.begin_user_turn("repeat")
    session.append_user_message("repeat")
    publisher.publish("reasoning.delta", {"piece": "Before tool"})
    call = ToolCall("read_file", {"path": "note.md"}, "call-1")
    session.record_assistant_turn("", {}, "tool_calls", [call], assistant_message={
        "role": "assistant", "content": "", "reasoning": "Before tool",
        "tool_calls": [{"id": call.id}],
    })
    publisher.publish("tool.planned", {"call_id": call.id, "name": call.name, "arguments": call.arguments})
    artifact = ArtifactRef("art-1", "text/plain", "note.md", 13, storage_path="private/note.md")
    result = ToolResult.success(
        "file contents", summary="Read note", content=({"type": "text", "text": "file contents"},),
        artifacts=(artifact,), retention="instruction", retention_key="note",
    )
    session.record_tool_execution(call.id, result)
    publisher.publish("tool.finished", {
        "call_id": call.id, "name": call.name, "arguments": call.arguments, **result.to_dict(),
    })
    publisher.publish("session.status_changed", {"session_id": publisher.session_id, "turn_signal": "model_turn_started"})
    publisher.publish("reasoning.delta", {"piece": "After tool"})
    handle = SessionHandle(runtime)
    captured = handle.snapshot()
    assert [item.get("text", item.get("call_id")) for item in captured["timeline"]] == [
        "repeat", "Earlier answer", "repeat", "Before tool", "call-1", "After tool",
    ]
    assert captured["active_turn"]["segment"] == 1
    expected_result = {**result.to_dict(), "artifacts": [
        {key: value for key, value in artifact.to_dict().items() if key != "storage_path"},
    ]}
    captured_tool = next(item for item in captured["timeline"] if item.get("call_id") == call.id)
    assert captured_tool["phase"] == "succeeded"
    assert captured_tool["result"] == expected_result
    publisher.publish("reasoning.delta", {"piece": " continued"})
    assert captured["timeline"][-1]["text"] == "After tool"
    publisher.publish("content.final", {"content": "Done"})
    session.record_assistant_turn("Done", {}, "final", assistant_message={
        "role": "assistant", "content": "Done", "reasoning": "After tool continued",
    })
    session.mark_completed()
    publisher.publish("turn.completed", {}, turn_id="turn-2")
    finished = handle.snapshot()
    assert finished["active_turn"] is None
    finished_tool = next(item for item in finished["timeline"] if item.get("call_id") == call.id)
    assert finished_tool["phase"] == captured_tool["phase"]
    assert finished_tool["result"] == captured_tool["result"]
    assert [item.get("text", item.get("call_id")) for item in finished["timeline"]] == [
        "repeat", "Earlier answer", "repeat", "Before tool", "call-1", "After tool continued", "Done",
    ]
    handle.close()


@pytest.fixture
def offline_manager(monkeypatch, tmp_path):
    """Use the real directory/checkpoint/service chain without a provider call."""
    from tests.responses import response
    from wright.application.composition import runtime as assembly

    calls = []

    class Model:
        model = "offline"
        transport_name = "chat"
        context_limit = 128_000

        def __call__(self, *_args, **_kwargs):
            calls.append("model")
            yield response(content="offline response")

    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_MODEL", "offline")
    monkeypatch.setattr(assembly, "load_env", lambda: None)
    monkeypatch.setattr(assembly, "LLMClient", lambda **_: Model())
    monkeypatch.setattr(assembly, "load_mcp_configs", lambda _: [])
    monkeypatch.setattr(assembly, "optional_knowledge_tools", list)
    monkeypatch.setattr(assembly, "load_lifecycle_manager", lambda *a, **k: SimpleNamespace(emit=lambda *a, **k: None))
    root = tmp_path / "project"
    root.mkdir()
    manager = RuntimeManager(root, capacity=1, base_args=argparse.Namespace())
    yield manager, calls
    manager.shutdown()


def _wait_completed(handle, command_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        record = handle.command_status(command_id)
        if record["status"] == "completed":
            return
        time.sleep(0.005)
    pytest.fail(f"command did not complete: {command_id}")


def test_history_resumes_same_conversation_and_only_executes_after_send(offline_manager):
    manager, calls = offline_manager
    original = manager.create(environment="local")
    session_id = original.session_id
    original.submit("remember this context", "first-command")
    _wait_completed(original, "first-command")
    original_state = original.runtime.session_state
    manager.close(session_id)
    preview = manager.preview(session_id)
    assert preview["session"]["active"] is False
    assert calls == ["model"]
    # The saved request breakdown must reach the read-only preview, or the
    # Context panel reports the session as having no statistics.
    assert preview["context_breakdown"] == (original_state.request_context_breakdown or None)
    assert preview["context_breakdown"]

    restored = manager.create(resume=session_id, client_request_id="resume-from-history")
    assert restored.session_id == session_id
    assert restored.runtime.session_state.environment == original_state.environment
    assert restored.runtime.session_state.workspace_dir == original_state.workspace_dir
    assert restored.runtime.session_state.authorization.permission_mode == original_state.authorization.permission_mode
    assert "remember this context" in str(restored.snapshot()["timeline"])
    assert "offline response" in str(restored.snapshot()["timeline"])
    assert calls == ["model"]  # Opening is never an implicit model turn.

    restored.submit("continue this conversation", "second-command")
    restored.submit("continue this conversation", "second-command")
    _wait_completed(restored, "second-command")
    assert calls == ["model", "model"]
    assert {row["session_id"] for row in manager.list_sessions()} == {session_id}


def test_repeated_and_concurrent_web_resume_reuses_one_live_session_at_capacity(offline_manager):
    from concurrent.futures import ThreadPoolExecutor

    manager, calls = offline_manager
    original = manager.create(environment="local")
    session_id = original.session_id
    assert manager.create(resume=session_id, client_request_id="other-tab") is original
    assert not original.closed
    manager.close(session_id)

    barrier = threading.Barrier(2)

    def resume(request_id):
        barrier.wait(timeout=2)
        return manager.create(resume=session_id, client_request_id=request_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(resume, name) for name in ("tab-a", "tab-b")]
        first, second = [future.result(timeout=5) for future in futures]
    assert first is second
    assert first.session_id == session_id
    assert len(manager.directory._sessions) == 1
    assert not first.closed
    assert calls == []


def test_all_project_history_has_stable_owners_and_explicit_actions_ignore_ui_selection(offline_manager, tmp_path):
    from wright.core.paths import project_id

    manager, calls = offline_manager
    launch = manager.create(environment="local")
    manager.close(launch.session_id)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_id = manager.register_workspace(str(other_root))["project_id"]
    other = manager.create_in_workspace(other_id, environment="local", model=None, prompt=None, resume=None)
    manager.close(other.session_id)
    manager.workspaces()  # Ensure launch project is registered for this test's explicit owner calls.
    launch_id = project_id(manager.project_root)
    manager.register_workspace(str(manager.project_root))
    manager.select_workspace(launch_id)

    rows = manager.list_sessions(all_projects=True)
    assert {(row["session_id"], row["project_id"], row["project_root"]) for row in rows} == {
        (launch.session_id, launch_id, str(manager.project_root)),
        (other.session_id, other_id, str(other_root)),
    }
    assert [row["session_id"] for row in manager.list_sessions()] == [launch.session_id]
    preview = manager.preview(other.session_id, registered_id=other_id)
    assert preview["session"]["project_id"] == other_id
    assert preview["session"]["project_root"] == str(other_root)
    manager.relabel(other.session_id, "Other project task", registered_id=other_id)
    assert manager.preview(other.session_id, registered_id=other_id)["session"]["user_goal"] == "Other project task"
    with pytest.raises(RuntimeManagerError) as error:
        manager.preview(other.session_id, registered_id=launch_id)
    assert error.value.status_code == 404
    restored = manager.create_in_workspace(other_id, environment=None, model=None, prompt=None, resume=other.session_id)
    assert restored.snapshot()["session"]["project_id"] == other_id
    assert restored.runtime.session_state.project_root == other_root
    assert calls == []


def test_worktree_history_restores_its_original_directory_and_missing_tree_stays_read_only(offline_manager):
    import shutil
    import subprocess

    manager, calls = offline_manager
    for args in (("init",), ("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "Initial")):
        subprocess.run(["git", "-C", str(manager.project_root), *args], check=True, capture_output=True)
    original = manager.create(environment="worktree")
    session_id = original.session_id
    worktree = original.runtime.session_state.workspace_dir
    assert worktree != manager.project_root
    original_branch = original.runtime.session_state.branch_name
    manager.close(session_id)
    restored = manager.create(resume=session_id)
    assert restored.session_id == session_id
    assert restored.runtime.session_state.workspace_dir == worktree
    assert restored.runtime.session_state.branch_name == original_branch
    assert restored.runtime.session_state.environment == "worktree"
    manager.close(session_id)
    shutil.rmtree(worktree)
    preview = manager.preview(session_id)
    assert preview["session"]["recoverable"] is False
    with pytest.raises(RuntimeManagerError, match="execution directory is no longer available") as error:
        manager.create(resume=session_id)
    assert error.value.status_code == 409
    assert not worktree.exists()
    assert calls == []


def test_missing_registered_project_reports_a_recoverable_owner_error(offline_manager, tmp_path):
    manager, calls = offline_manager
    missing = tmp_path / "removed-project"
    missing.mkdir()
    owner = manager.register_workspace(str(missing))["project_id"]
    missing.rmdir()
    with pytest.raises(RuntimeManagerError, match="workspace does not exist") as error:
        manager.preview("saved", registered_id=owner)
    assert error.value.status_code == 409
    assert calls == []
