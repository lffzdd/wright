"""Same application intent from CLI, TUI, and Web entry points."""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from wright.application.session.controls import apply_control, interpret_control
from wright.application.session.directory import SessionDirectory, SessionDirectoryError
from wright.application.session.opening import (
    load_resume_checkpoint,
    resolve_new_environment,
)
from wright.application.session.publisher import EventPublisher
from wright.application.session.service import SessionService
from wright.application.workspace.references import (
    active_mention_query,
    capture_references,
    identify_reference,
    parse_explicit_mentions,
    prepare_submission_references,
    search_file_references,
)
from wright.core.paths import project_id
from wright.interfaces.cli.args import parse_cli_args


def test_new_sessions_default_local_and_worktree_is_explicit(tmp_path):
    assert resolve_new_environment(None, git=True) == "local"
    assert resolve_new_environment("", git=False) == "local"
    assert resolve_new_environment("worktree", git=True) == "worktree"
    with pytest.raises(Exception, match="git"):
        resolve_new_environment("worktree", git=False)
    directory = SessionDirectory(tmp_path)
    assert directory.project()["default_environment"] == "local"
    with pytest.raises(SessionDirectoryError, match="git") as caught:
        directory.open(environment="worktree")
    assert caught.value.kind == "environment"


def test_resume_intent_survives_blank_and_continue(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright", "--resume"])
    blank = parse_cli_args()
    assert blank.resume == ""
    assert blank.continue_latest is False
    monkeypatch.setattr(sys, "argv", ["wright", "--resume", "saved-id"])
    chosen = parse_cli_args()
    assert chosen.resume == "saved-id"
    monkeypatch.setattr(sys, "argv", ["wright", "--continue"])
    continued = parse_cli_args()
    assert continued.continue_latest is True
    assert continued.resume is None

    class Checkpoints:
        def load_latest(self):
            return SimpleNamespace(session_id="latest", environment="worktree")

        def list_recent_sessions(self, limit=12):
            return [{"session_id": "picked", "environment": "local"}]

        def load(self, session_id):
            return SimpleNamespace(session_id=session_id, environment="worktree")

    store = Checkpoints()
    assert load_resume_checkpoint(store, resume=None, continue_latest=False, resume_chooser=None) is None
    latest = load_resume_checkpoint(store, resume=None, continue_latest=True, resume_chooser=None)
    assert latest.environment == "worktree"
    chosen_saved = load_resume_checkpoint(
        store, resume="", continue_latest=False, resume_chooser=lambda rows: rows[0]["session_id"],
    )
    assert chosen_saved.session_id == "picked"
    assert chosen_saved.environment == "worktree"
    explicit = load_resume_checkpoint(store, resume="saved-id", continue_latest=False, resume_chooser=None)
    assert explicit.session_id == "saved-id"


def test_stop_controls_share_one_scope(tmp_path):
    runtime = _runtime(tmp_path)
    started = threading.Event()
    release = threading.Event()
    seen: list[str] = []

    def consume(_runtime, event_type, payload):
        if event_type != "USER_INPUT":
            return event_type == "EXIT"
        seen.append(payload["prompt"])
        if payload["prompt"] == "first":
            started.set()
            release.wait(1)
        return False

    service = SessionService(runtime, event_processor=consume, shutdown=lambda _runtime: None)
    service.start()
    try:
        service.submit("first", "one")
        assert started.wait(1)
        service.submit("second", "two")
        service.submit("third", "three")
        stop = interpret_control("/stop")
        assert stop is not None and stop.name == "stop_current"
        apply_control(service, stop)
        assert "two" in service._queued and "two" not in service._cancelled
        assert "three" in service._queued
        stop_all = interpret_control("/stop-all")
        assert stop_all is not None
        apply_control(service, stop_all)
        assert {"two", "three"} <= service._cancelled
        assert "two" not in seen
    finally:
        release.set()
        service.close(wait_timeout=1)


def test_file_references_are_identity_until_execution(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("original", encoding="utf-8")
    spaced = project / "my notes.md"
    spaced.write_text("spaced", encoding="utf-8")
    (project / "blob.bin").write_bytes(b"a\0b")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    assert active_mention_query("mail ada@example.com") is None
    assert active_mention_query("see @notes") == "notes"
    assert parse_explicit_mentions("mail me at ada@example.com and see @notes") == []
    assert parse_explicit_mentions("read @[my notes.md]") == ["my notes.md"]
    candidates = search_file_references(project, "notes")
    assert candidates
    assert "text" not in candidates[0]
    with pytest.raises(Exception, match="authorized"):
        identify_reference(project, str(outside))
    external = identify_reference(project, str(outside), external_roots=[str(tmp_path)])
    assert external["external"] is True

    prepared = prepare_submission_references(
        project,
        [{"path": "notes.md"}],
        "also @[my notes.md] and ada@example.com",
    )
    assert [item["path"] for item in prepared] == ["notes.md", "my notes.md"]
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "notes.md").write_text("baseline", encoding="utf-8")
    (project / "notes.md").write_text("changed after queue", encoding="utf-8")
    captures = capture_references(
        project_root=project,
        execution_root=worktree,
        references=prepared,
    )
    by_path = {item["path"]: item for item in captures}
    assert by_path["notes.md"]["status"] == "text"
    assert by_path["notes.md"]["text"] == "baseline"
    assert by_path["my notes.md"]["status"] == "missing"
    binary = capture_references(
        project_root=project,
        execution_root=project,
        references=[{"kind": "file", "path": "blob.bin", "name": "blob.bin", "project_id": project_id(project), "external": False}],
    )
    assert binary[0]["status"] == "binary"
    assert binary[0]["text"] is None


def test_same_request_reopens_the_same_session(tmp_path, monkeypatch):
    from wright.application.session.service import SessionService as Service

    monkeypatch.setattr(Service, "start", lambda self: None)
    monkeypatch.setattr(
        "wright.application.session.directory.shutdown_runtime", lambda _runtime: None,
    )
    assembled: list[str] = []

    def assemble(_config, *, session_id=None, application_host=None, publisher=None, project_context=None, **_kwargs):
        assembled.append(session_id)
        idle = threading.Event()
        idle.set()
        state = SimpleNamespace(
            session_id=session_id,
            lifecycle="open",
            model_name="test",
            environment=project_context.environment,
            workspace_dir=project_context.execution_root,
            project_root=project_context.project_root,
            base_commit="",
            branch_name="",
            user_goal="",
            message_records=[],
            turns=[],
            runs={},
            attachments={},
            documents={},
            current_run_status=lambda: "idle",
            active_run=lambda: None,
            attachment_records=lambda _ids: [],
            task_usage=lambda: SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            plan_manager=SimpleNamespace(has_plan=False, snapshot=lambda: {"steps": []}),
            request_context_tokens=0,
            context_tokens=0,
            permission_mode="default",
        )
        return SimpleNamespace(
            application_host=application_host or SimpleNamespace(workspace_dir=project_context.execution_root, close=lambda: True, has_active_work=lambda: False, state="open"),
            owns_application_host=application_host is None,
            session_state=state,
            agent=SimpleNamespace(checkpoint_store=None, on_run_started=None),
            publisher=publisher or EventPublisher(project_id="project", session_id=session_id),
            interaction_broker=None,
            event_queue=queue.Queue(),
            agent_idle=idle,
            cancellation_event=threading.Event(),
            autonomy_store=None,
            resumed=False,
            project_context=project_context,
            permission_settings=SimpleNamespace(mode="default", additional_directories=[]),
        )

    directory = SessionDirectory(tmp_path, assemble=assemble)
    first = directory.open(environment="local", client_request_id="request-1")
    second = directory.open(environment="worktree", client_request_id="request-1")
    assert first.session_id == second.session_id
    assert assembled == [first.session_id]
    assert first.runtime.session_state.environment == "local"
    directory.shutdown()


def test_cli_session_switch_moves_the_renderer_listener():
    from wright.interfaces.cli.input import CliInputController

    class Publisher:
        def __init__(self) -> None:
            self.removed: list[str] = []
            self.listeners: dict[str, object] = {}

        def add_listener(self, listener: object) -> str:
            self.listeners["next"] = listener
            return "next"

        def remove_listener(self, listener_id: str) -> None:
            self.removed.append(listener_id)

    class Hub:
        def __init__(self) -> None:
            self.interrupt = None

        def bind_collector(self, interrupt=None) -> None:
            self.interrupt = interrupt

    previous = Publisher()
    opened_publisher = Publisher()
    old_hub = Hub()
    notices: list[str] = []

    class Renderer:
        def on_system_notice(self, text: str, **_kwargs) -> None:
            notices.append(text)

        def render_session_history(self, _state) -> None:
            notices.append("history")

    class Service:
        publisher = previous

    opened = SimpleNamespace(
        service=SimpleNamespace(publisher=opened_publisher),
        publisher=opened_publisher,
        session_id="saved",
        runtime=SimpleNamespace(
            agent_idle=threading.Event(),
            resumed=True,
            session_state=SimpleNamespace(environment="worktree"),
        ),
    )

    class Directory:
        def open(self, **_kwargs):
            return opened

    controller = CliInputController(
        service=Service(),
        agent_idle=threading.Event(),
        renderer=Renderer(),  # type: ignore[arg-type]
        hub=old_hub,  # type: ignore[arg-type]
        directory=Directory(),
        listener_id="old",
    )
    controller._switch_session("resume", "saved")
    assert previous.removed == ["old"]
    assert "next" in opened_publisher.listeners
    assert old_hub.interrupt is not None
    assert controller._hub is not old_hub
    assert notices[0] == "history"
    assert "worktree" in notices[1]


def _runtime(root: Path):
    idle = threading.Event()
    idle.set()
    state = SimpleNamespace(
        session_id="session",
        status="open",
        model_name="test",
        user_goal="test",
        environment="local",
        workspace_dir=root,
        project_root=root,
        base_commit=None,
        branch_name=None,
        llm_transport="chat",
        lifecycle="open",
        attachment_records=lambda ids: [],
        message_records=[],
        turns=[],
        runs={},
        plan_manager=SimpleNamespace(has_plan=False, snapshot=lambda: {"steps": []}),
        current_run_status=lambda: "idle",
        task_usage=lambda: SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        request_context_tokens=0,
        context_tokens=0,
    )
    return SimpleNamespace(
        session_state=state,
        publisher=EventPublisher(project_id="project", session_id="session"),
        interaction_broker=None,
        event_queue=queue.Queue(),
        agent_idle=idle,
        cancellation_event=threading.Event(),
        llm=SimpleNamespace(model="test"),
        agent=SimpleNamespace(llm=SimpleNamespace(model="test"), checkpoint_store=None, continue_run=lambda: None, on_run_started=None),
        resumed=False,
        project_context=SimpleNamespace(project_root=root, execution_root=root),
        permission_settings=SimpleNamespace(additional_directories=[]),
    )
