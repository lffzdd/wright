"""Session directory, directory leases, history identity, and interaction mailbox."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wright.application.command.execution import CommandExecution
from wright.application.composition.host import ApplicationHost
from wright.application.execution.directory import (
    DirectoryExecutionCoordinator,
    bind_directory_work,
    reset_directory_work,
)
from wright.application.execution.identity import ExecutionIdentity
from wright.application.session.directory import SessionDirectory, SessionDirectoryError
from wright.application.session.history_projection import project_history
from wright.application.session.mailbox import InteractionMailbox
from wright.application.session.publisher import EventPublisher
from wright.application.session.service import SessionService
from wright.domain.policy import PermissionSettings
from wright.infrastructure.persistence.autonomy_store import AutonomyStore


def _state(session_id: str, root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        lifecycle="open",
        model_name="test",
        environment="local",
        workspace_dir=root,
        project_root=root,
        base_commit="",
        branch_name="",
        user_goal="",
        message_records=[],
        turns=[],
        runs={},
        attachments={},
        tool_executions={},
        current_run_status=lambda: "idle",
        active_run=lambda: None,
        attachment_records=lambda _ids: [],
        task_usage=lambda: SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        plan_manager=SimpleNamespace(has_plan=False, snapshot=lambda: {"steps": []}),
        request_context_tokens=0,
        context_tokens=0,
        agent_root_turn_id="",
        register_command=lambda _record: None,
    )


def _runtime(session_id: str, root: Path, coordinator, agent_run, *, store=None) -> SimpleNamespace:
    idle = threading.Event()
    idle.set()
    return SimpleNamespace(
        session_state=_state(session_id, root),
        publisher=EventPublisher(project_id="project", session_id=session_id),
        interaction_broker=None,
        event_queue=queue.Queue(),
        agent_idle=idle,
        cancellation_event=threading.Event(),
        autonomy_store=store,
        resumed=False,
        project_context=SimpleNamespace(execution_root=root),
        directory_coordinator=coordinator,
        agent=SimpleNamespace(checkpoint_store=None, run=agent_run),
        services=SimpleNamespace(
            loop_registry=None, autonomy_scheduler=None, agent_background=None,
        ),
        event_renderer=SimpleNamespace(on_system_notice=lambda _text: None),
        draft_attachments=None,
        llm=SimpleNamespace(context_limit=100, model="test"),
        application_host=None,
        owns_application_host=False,
    )


def _service(runtime) -> SessionService:
    service = SessionService(runtime, shutdown=lambda _runtime: None)
    service.start()
    return service


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _scripted_open_runtime(session_id: str, root: Path, host, broker, publisher) -> SimpleNamespace:
    runtime = _runtime(session_id, root, None, lambda *_args, **_kwargs: None)
    runtime.application_host = host
    runtime.interaction_broker = broker
    if publisher is not None:
        runtime.publisher = publisher
    runtime.project_context = SimpleNamespace(execution_root=root)
    return runtime


class _Host:
    def __init__(self) -> None:
        self.state = "new"
        self.starts = 0
        self.closed = 0

    def start(self) -> None:
        if self.state == "running":
            return
        self.state = "running"
        self.starts += 1

    def close(self) -> bool:
        self.closed += 1
        self.state = "closed"
        return True

    def has_active_work(self) -> bool:
        return False


def test_same_directory_turns_queue_and_cancel_does_not_stop_the_holder(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    coordinator = DirectoryExecutionCoordinator()
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def run_first(_prompt, **_kwargs):
        entered.set()
        release.wait(timeout=2)

    def run_second(_prompt, **_kwargs):
        second_entered.set()

    first = _service(_runtime("session-a", root, coordinator, run_first))
    second = _service(_runtime("session-b", root, coordinator, run_second))
    try:
        first.submit("do the work", "command-a")
        assert entered.wait(timeout=2)
        second.submit("later work", "command-b")
        queued = False
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            summary = second.summary()
            if summary["execution"] == "queued" and summary["queue_reason"]:
                queued = True
                break
            time.sleep(0.02)
        assert queued, second.summary()
        assert "directory" in second.summary()["queue_reason"]
        assert first.summary()["execution"] == "running"
        second.cancel_current("cancel-b")
        time.sleep(0.2)
        assert not second_entered.is_set()
        assert coordinator.occupied(root)
        assert first.summary()["execution"] == "running"
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and coordinator.occupied(root):
            time.sleep(0.02)
        assert not coordinator.occupied(root)
        assert not second_entered.is_set()
    finally:
        release.set()
        first.close(wait_timeout=2)
        second.close(wait_timeout=2)


def test_queued_turn_starts_after_the_holder_releases(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    coordinator = DirectoryExecutionCoordinator()
    release = threading.Event()
    order: list[str] = []

    def run_first(_prompt, **_kwargs):
        order.append("a-start")
        release.wait(timeout=2)
        order.append("a-end")

    def run_second(_prompt, **_kwargs):
        order.append("b-start")

    first = _service(_runtime("session-a", root, coordinator, run_first))
    second = _service(_runtime("session-b", root, coordinator, run_second))
    try:
        first.submit("first", "command-a")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and "a-start" not in order:
            time.sleep(0.02)
        second.submit("second", "command-b")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and second.summary()["execution"] != "queued":
            time.sleep(0.02)
        assert second.summary()["execution"] == "queued"
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and "b-start" not in order:
            time.sleep(0.02)
        assert order == ["a-start", "a-end", "b-start"]
    finally:
        release.set()
        first.close(wait_timeout=2)
        second.close(wait_timeout=2)


def test_different_directories_overlap(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    coordinator = DirectoryExecutionCoordinator()
    barrier = threading.Barrier(2, timeout=2)
    overlapped: list[str] = []
    errors: list[BaseException] = []

    def run(name: str):
        def _run(_prompt, **_kwargs):
            try:
                barrier.wait()
                overlapped.append(name)
            except BaseException as exc:
                errors.append(exc)
        return _run

    first = _service(_runtime("session-a", left, coordinator, run("a")))
    second = _service(_runtime("session-b", right, coordinator, run("b")))
    try:
        first.submit("left", "command-a")
        second.submit("right", "command-b")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(overlapped) < 2 and not errors:
            time.sleep(0.02)
        assert errors == []
        assert sorted(overlapped) == ["a", "b"]
    finally:
        first.close(wait_timeout=2)
        second.close(wait_timeout=2)


def test_child_lease_does_not_deadlock_and_background_keeps_occupancy(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    coordinator = DirectoryExecutionCoordinator()
    parent = coordinator.acquire(
        root, kind="turn", holder_id="parent", session_id="session-a", label="session session-a",
    )
    token = bind_directory_work(coordinator, parent)
    started = threading.Event()

    def wait_for_directory():
        try:
            lease = coordinator.acquire(
                root, kind="turn", holder_id="other", session_id="session-b",
                label="session session-b",
            )
            started.set()
            lease.release()
        except Exception:
            return

    waiter = threading.Thread(target=wait_for_directory)
    waiter.start()
    time.sleep(0.15)
    child = coordinator.retain(
        parent.lease_id, kind="subagent", holder_id="child", session_id="session-a",
        label="subagent child",
    )
    assert not started.is_set()
    parent.release()
    time.sleep(0.15)
    assert coordinator.occupied(root)
    assert not started.is_set()
    child.release()
    waiter.join(2)
    assert started.is_set()
    assert not coordinator.occupied(root)
    reset_directory_work(token)

    parent = coordinator.acquire(
        root, kind="turn", holder_id="parent-2", session_id="session-a", label="session session-a",
    )
    token = bind_directory_work(coordinator, parent)
    session = _state("session-a", root)
    commands = CommandExecution(
        session,
        ExecutionIdentity(
            has_agent=lambda _value: False,
            has_command=lambda _value: False,
            has_run=lambda _value: False,
            has_schedule=lambda _value: False,
        ),
    )
    commands.attach_directory(coordinator)
    try:
        commands._retain_background("cmd_live")
        parent.release()
        assert coordinator.occupied(root)
        describe = coordinator.describe(root)
        assert describe["holders"][0]["kind"] == "background"
        commands._release_background("cmd_live")
        assert not coordinator.occupied(root)
    finally:
        reset_directory_work(token)
        commands.close()


def test_waiting_for_permission_still_allows_other_session_queries(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    coordinator = DirectoryExecutionCoordinator()
    from wright.interfaces.interaction import InteractionHub

    hub = InteractionHub()
    waiting = threading.Event()

    def run_first(_prompt, **_kwargs):
        waiting.set()
        hub.request("permission", {"tool_name": "shell"})

    def run_second(_prompt, **_kwargs):
        raise AssertionError("queued session must not start while the holder waits")

    first_runtime = _runtime("session-a", root, coordinator, run_first)
    first_runtime.interaction_broker = hub
    first = _service(first_runtime)
    second = _service(_runtime("session-b", root, coordinator, run_second))
    try:
        first.submit("needs permission", "command-a")
        assert waiting.wait(timeout=2)
        second.submit("queued", "command-b")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and second.summary()["execution"] != "queued":
            time.sleep(0.02)
        assert second.summary()["execution"] == "queued"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not hub.has_pending():
            time.sleep(0.02)
        assert first.summary()["execution"] == "waiting_for_input"
        pending = hub.poll()
        assert pending is not None
        second.cancel_current("cancel-b")
        time.sleep(0.2)
        assert coordinator.occupied(root)
        assert hub.resolve(pending.request_id, "allow_once")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and coordinator.occupied(root):
            time.sleep(0.02)
        assert not coordinator.occupied(root)
    finally:
        hub.close()
        first.close(wait_timeout=2)
        second.close(wait_timeout=2)


def test_automation_waits_behind_an_interactive_lease(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AutonomyStore(tmp_path / "tasks.sqlite3", session_id="origin", workspace_dir=workspace)
    coordinator = DirectoryExecutionCoordinator()
    launched: list[dict] = []

    def fake_launch(**kwargs):
        launched.append(kwargs)
        lease = kwargs.get("directory_lease")
        if lease is not None:
            lease.release()

    monkeypatch.setattr(
        "wright.application.composition.host.launch_durable_run", fake_launch,
    )
    host = ApplicationHost(
        workspace_dir=workspace,
        store=store,
        llm=SimpleNamespace(model="test"),
        base_tools=[],
        permission_settings=PermissionSettings(),
        directory_coordinator=coordinator,
    )
    holder = coordinator.acquire(
        workspace, kind="turn", holder_id="interactive", session_id="session-a",
        label="session session-a",
    )
    thread = threading.Thread(
        target=host._launch_when_directory_free,
        args=("run-queued", host.scheduler, coordinator),
        daemon=True,
    )
    thread.start()
    time.sleep(0.2)
    assert launched == []
    assert coordinator.describe(workspace)["queue"]
    holder.release()
    thread.join(2)
    assert len(launched) == 1
    assert launched[0]["directory_lease"] is not None
    host.close()
    store.close()


def test_two_local_sessions_share_a_host_and_capacity_does_not_close_them(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "wright.application.session.directory.shutdown_runtime", lambda _runtime: None,
    )
    created: list[_Host] = []

    def assemble(_config, *, session_id=None, application_host=None, interaction_broker=None, publisher=None, project_context=None, **_kwargs):
        host = application_host or _Host()
        if application_host is None:
            created.append(host)
        host.start()
        root = project_context.execution_root
        return _scripted_open_runtime(session_id, root, host, interaction_broker, publisher)

    directory = SessionDirectory(tmp_path, capacity=2, assemble=assemble)
    first = directory.open(environment="local")
    second = directory.open(environment="local")
    try:
        assert first.runtime.application_host is second.runtime.application_host
        assert created[0].starts == 1
        with pytest.raises(SessionDirectoryError, match="capacity") as caught:
            directory.open(environment="local")
        assert caught.value.kind == "capacity"
        assert len([item for item in directory.list_sessions() if item["active"]]) == 2
        with pytest.raises(SessionDirectoryError, match="open session"):
            directory.archive(second.session_id)
        assert created[0].closed == 0
        assert first.session_id in {
            item["session_id"] for item in directory.list_sessions() if item["active"]
        }
        directory.close(first.session_id)
        assert created[0].closed == 0
    finally:
        directory.shutdown()
    assert created[0].closed == 1
    directory.close(first.session_id)
    directory.close("missing")


def test_failed_open_rolls_back_a_new_worktree(tmp_path, monkeypatch):
    _git_repo(tmp_path)
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))

    def assemble(*_args, **_kwargs):
        raise RuntimeError("assembly failed")

    directory = SessionDirectory(tmp_path, assemble=assemble)
    with pytest.raises(RuntimeError, match="assembly failed"):
        directory.open(environment="worktree")
    listing = subprocess.run(
        ["git", "-C", str(tmp_path), "worktree", "list"],
        check=True, capture_output=True, text=True,
    )
    assert listing.stdout.strip().count("\n") == 0


def test_history_keeps_markup_user_text_and_hides_storage(tmp_path):
    del tmp_path
    turn = SimpleNamespace(
        step=1, step_id="turn-html", run_id="run-html", route="final", error=None,
        message_id="msg-html", parsed={"final_answer": ""},
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
    )
    cancelled = SimpleNamespace(
        run_id="run-cancelled", status="cancelled", goal="<div>goal</div>", result="",
        error="", tool_execution_ids=["call-1"],
    )
    execution = SimpleNamespace(
        call=SimpleNamespace(id="call-1", name="write_file", arguments={"file": "a.txt"}),
        status="succeeded",
        result=SimpleNamespace(to_dict=lambda: {
            "ok": True,
            "storage_path": "/secret/a.txt",
            "artifacts": [{"id": "art", "storage_path": "/secret/art"}],
        }),
    )
    session = SimpleNamespace(
        turns=[turn],
        runs={"run-html": SimpleNamespace(run_id="run-html", status="completed", tool_execution_ids=[]),
              "run-cancelled": cancelled},
        tool_executions={"call-1": execution},
        message_records=[
            SimpleNamespace(
                id="internal", source="system_feedback",
                message={"role": "user", "content": "<system>hidden</system>", "attachments": []},
            ),
            SimpleNamespace(
                id="user-html", source="user_input",
                message={"role": "user", "content": "<div>hello</div>", "attachments": ["att-1"]},
            ),
            SimpleNamespace(id="msg-html", source="model_output", message={"role": "assistant", "content": ""}),
        ],
        attachment_records=lambda ids: [
            SimpleNamespace(to_dict=lambda: {
                "id": "att-1", "filename": "pic.png", "width": 1, "height": 1,
                "storage_path": "/secret/pic.png",
            })
        ] if ids else [],
    )
    history = project_history(session, {"turn-html"}, {"run-cancelled"})
    assert history[0]["user"] == "<div>hello</div>"
    assert history[0]["message_id"] == "user-html"
    assert history[0]["assistant"] == ""
    assert history[0]["attachments"][0]["filename"] == "pic.png"
    assert "storage_path" not in history[0]["attachments"][0]
    assert history[1]["status"] == "cancelled"
    assert history[1]["user"] == "<div>goal</div>"
    assert "storage_path" not in history[1]["tools"][0]
    assert "storage_path" not in history[1]["tools"][0]["artifacts"][0]


def test_mailbox_fail_closed_and_no_double_wake():
    mailbox = InteractionMailbox(delivers=False)
    assert mailbox.request("permission", {"tool_name": "shell"}) == "deny"
    assert mailbox.request("ask_user", {"question": "x"}) is None

    durable = InteractionMailbox()

    def fail_record(*_args):
        raise RuntimeError("disk full")

    durable.set_persistence(fail_record, lambda *_args: None)
    with pytest.raises(RuntimeError, match="disk full"):
        durable.request("permission", {"tool_name": "shell"})
    assert durable.snapshot() == []

    wakes = {"resolved": 0}

    def fail_resolve(*_args):
        wakes["resolved"] += 1
        raise RuntimeError("cannot commit")

    retry = InteractionMailbox()
    retry.set_persistence(lambda *_args: None, fail_resolve)
    item = retry.begin("permission", {"tool_name": "shell"})
    assert item is not None
    assert retry.resolve(item.request_id, "allow_once") is False
    assert not item.done.is_set()
    assert wakes["resolved"] == 1
    assert retry.snapshot()

    late = InteractionMailbox()
    pending = late.begin("ask_user", {"question": "name"})
    assert pending is not None
    assert late.resolve(pending.request_id, "ada")
    assert late.resolve(pending.request_id, "ada") is False
    late.cancel_pending()
    assert pending.done.is_set()

    closed = InteractionMailbox()
    waiting = closed.begin("permission", {"tool_name": "shell"})
    assert waiting is not None
    closed.close()
    assert waiting.done.is_set()
    assert closed.begin("permission", {"tool_name": "shell"}) is None
    assert closed.resolve(waiting.request_id, "allow_once") is False
