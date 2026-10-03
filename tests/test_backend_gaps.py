"""Contracts behind edit cards, plan details and effective permission panels."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from wright.application.planning.projection import bind_plan_events, project_plan
from wright.application.session.publisher import EventPublisher
from wright.application.tool_execution.runtime import tool_runtime_for_session
from wright.application.workspace.grants import list_grants
from wright.domain.gateway.execution import ExecutionPath
from wright.domain.model.planning import PlanError, PlanManager
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolAccess, ToolCall
from wright.domain.policy.permission.resolver import (
    PermissionPolicy,
    PermissionResolver,
)
from wright.domain.policy.permission.scope import AccessScope
from wright.domain.policy.permission.settings import (
    MatchContext,
    PermissionRule,
    PermissionSettings,
)
from wright.domain.policy.permission.types import InvocationIdentity, PermissionSubject
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.runtime.local import LocalExecutionBackend
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.file import (
    edit_file,
    edit_file_tool,
    read_file,
    read_file_tool,
)
from wright.infrastructure.tools.file.diff import MAX_DIFF_BYTES, edit_diff
from wright.interfaces.interaction import InteractionBroker


@pytest.mark.parametrize(
    ("before", "after", "adds", "dels"),
    [
        ("a\nold\nz\n", "a\nnew\nz\n", 1, 1),
        ("old\nx\nold\n", "new\nx\nnew\n", 2, 2),
        ("a\nb\nc\n", "a\none\ntwo\nc\n", 2, 1),
        ("a\n\nb\n", "a\nb\n", 0, 1),
        ("a\r\nb\r\n", "a\r\nc\r\n", 1, 1),
        ("old", "new", 1, 1),
        ("same", "same\n", 1, 1),
        ("--old\n", "++new\n", 1, 1),
        ("same\n", "same\n", 0, 0),
    ],
)
def test_edit_diff_covers_real_line_changes(before, after, adds, dels):
    result = edit_diff(before, after, "file.txt")
    assert (result["additions"], result["deletions"]) == (adds, dels)
    assert result["diff_truncated"] is False
    if adds or dels:
        assert result["diff"].startswith("--- a/file.txt\n+++ b/file.txt\n@@ ")
    else:
        assert result["diff"] == ""
    assert "\r" not in result["diff"]
    if before != after and (not before.endswith("\n") or not after.endswith("\n")):
        assert "\\ No newline at end of file\n" in result["diff"]


def test_diff_hunks_track_offsets_and_three_context_lines():
    before = "".join(f"line {i}\n" for i in range(1, 31))
    after = before.replace("line 5\n", "replacement\nextra\n").replace(
        "line 25\n", "last\n"
    )
    diff = edit_diff(before, after, "file.txt")["diff"]
    assert "@@ -2,7 +2,8 @@" in diff
    assert "@@ -22,7 +23,7 @@" in diff
    assert " line 1\n" not in diff and " line 30\n" not in diff


def test_diff_truncates_at_complete_utf8_rows_with_full_counts():
    before = "".join(f"old {i}\n" for i in range(1500))
    after = "".join(f"中文 {i} {'x' * 70}\n" for i in range(1500))
    result = edit_diff(before, after, "file.txt")
    assert result["diff_truncated"] is True
    assert len(result["diff"].encode("utf-8")) <= MAX_DIFF_BYTES
    assert result["diff"].endswith("\n")
    assert "\\ Diff truncated" in result["diff"]
    assert result["additions"] == result["deletions"] == 1500
    long_line = edit_diff("a\n", "x" * MAX_DIFF_BYTES + "\n", "file.txt")
    assert long_line["diff_truncated"] is True and long_line["additions"] == 1
    assert "+xx" not in long_line["diff"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_successful_edit_result_and_bytes_agree(tmp_path, newline):
    path = tmp_path / "file.txt"
    before = f"first{newline}old{newline}last"
    path.write_bytes(before.encode())
    runtime = tool_runtime_for_session(
        Session.create("edit", tmp_path), workspace_dir=tmp_path
    )
    assert read_file("file.txt", runtime=runtime).ok
    result = edit_file("file.txt", "old", f"new{newline}extra", runtime=runtime)
    assert result.ok
    after = before.replace("old", f"new{newline}extra")
    assert path.read_bytes() == after.encode()
    assert result.data["additions"] == 2 and result.data["deletions"] == 1
    assert "@@ -1,3 +1,4 @@" in result.data["diff"]
    no_change = edit_file("file.txt", "new", "new", runtime=runtime)
    assert no_change.ok and no_change.data["diff"] == ""
    failure = edit_file("file.txt", "missing", "new", runtime=runtime)
    assert not failure.ok and "diff" not in failure.data


def test_plan_timestamps_are_stable_across_pause_resume_and_restore(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("wright.domain.model.planning.plan.time.time", lambda: now[0])
    manager = PlanManager()
    manager.create_plan("goal", ["first", "second"])
    assert manager.snapshot()["steps"][0]["started_at"] is None
    manager.update_step("step_1", "in_progress")
    now[0] = 102.0
    manager.update_step("step_1", "in_progress", note="working")
    assert manager.snapshot()["steps"][0]["started_at"] == 100.0
    manager.update_step("step_1", "blocked", note="external dependency")
    view = project_plan(manager.snapshot(), [])
    assert view["steps"][0]["elapsed_ms"] == 2000
    assert view["steps"][0]["wait_reason"]["kind"] == "blocked"
    restored = PlanManager.from_snapshot(manager.snapshot())
    now[0] = 105.0
    restored.update_step("step_1", "in_progress")
    restored.update_step("step_1", "completed")
    completed = restored.snapshot()["steps"][0]
    now[0] = 110.0
    restored.update_step("step_1", "completed", note="done")
    assert restored.snapshot()["steps"][0]["ended_at"] == completed["ended_at"] == 105.0
    assert project_plan(restored.snapshot(), [])["steps"][0]["elapsed_ms"] == 5000
    restored.replan(["new route"], reason="changed")
    assert restored.snapshot()["steps"][1]["ended_at"] == 110.0
    assert restored.snapshot()["steps"][2]["started_at"] is None
    assert manager.snapshot()["steps"][0]["ended_at"] is None


def test_unstarted_terminal_steps_do_not_invent_elapsed_and_invalid_updates_are_atomic():
    manager = PlanManager()
    manager.create_plan("goal", ["first", "second"])
    original = manager.snapshot()
    with pytest.raises(PlanError):
        manager.update_step("step_1", "in_progress", note="x" * 241)
    assert manager.snapshot() == original
    manager.update_step("step_1", "completed")
    manager.update_step("step_2", "skipped")
    assert all(
        "elapsed_ms" not in step
        for step in project_plan(manager.snapshot(), [])["steps"]
    )


@pytest.mark.parametrize("bad", [True, -1, float("inf"), float("nan"), "100"])
def test_plan_restore_rejects_invalid_timestamps_transactionally(bad):
    manager = PlanManager()
    snapshot = manager.create_plan("goal", ["first"])
    snapshot["steps"][0]["started_at"] = bad
    with pytest.raises(PlanError):
        manager.restore(snapshot)
    assert manager.snapshot()["steps"][0]["started_at"] is None


def test_history_timing_freezes_at_checkpoint_observation(tmp_path, monkeypatch):
    manager = PlanManager()
    monkeypatch.setattr("wright.domain.model.planning.plan.time.time", lambda: 100.0)
    manager.create_plan("goal", ["first"])
    manager.update_step("step_1", "in_progress")
    session = Session.create("goal", tmp_path)
    session.plan_manager = manager
    repository = FileSessionRepository(tmp_path / "checkpoints")
    repository.save(session)
    restored, saved_at = repository.load_with_saved_at(session.session_id)
    monkeypatch.setattr(
        "wright.domain.model.planning.plan.time.time", lambda: saved_at + 500
    )
    historic = project_plan(restored.plan_manager.snapshot(), [], observed_at=saved_at)
    assert historic["steps"][0]["elapsed_ms"] == int((saved_at - 100) * 1000)
    assert historic["observed_at"] == saved_at
    assert restored.plan_manager.snapshot() == session.plan_manager.snapshot()


def test_wait_projection_preserves_step_state_and_isolates_actual_principals():
    manager = PlanManager()
    manager.create_plan("goal", ["first"])
    manager.update_step("step_1", "in_progress")
    pending = [
        {"request_id": "root-p", "kind": "permission", "principal": "session"},
        {"request_id": "root-q", "kind": "ask_user"},
        {"request_id": "child", "kind": "permission", "principal": "child-task"},
        {"request_id": "unassigned", "kind": "permission", "agent_depth": 1},
    ]
    view = project_plan(manager.snapshot(), pending, owner_session_id="session")
    step = view["steps"][0]
    assert step["status"] == "in_progress"
    assert step["wait_reason"] == {
        "kind": "user_input",
        "request_ids": ["root-p", "root-q"],
    }
    assert view["wait_reason"]["request_ids"] == ["child", "unassigned"]
    assert "wait_reason" not in manager.snapshot()["steps"][0]
    cleared = project_plan(view, [], owner_session_id="session")
    assert "wait_reason" not in cleared and "wait_reason" not in cleared["steps"][0]
    manager.update_step("step_1", "pending")
    waiting_session = project_plan(
        manager.snapshot(), pending, owner_session_id="session"
    )
    assert len(waiting_session["wait_reason"]["request_ids"]) == 4
    assert "wait_reason" not in waiting_session["steps"][0]


def test_broker_updates_plan_on_permission_resolution_and_cancel(tmp_path):
    session = Session.create("goal", tmp_path, session_id="session")
    session.plan_manager.create_plan("goal", ["first"])
    session.plan_manager.update_step("step_1", "in_progress")
    publisher = EventPublisher(project_id="p", session_id="session")
    broker = InteractionBroker(publisher)
    bind_plan_events(publisher, session, broker)
    _, events = publisher.subscribe()
    answers = []

    def request():
        answers.append(
            broker.request(
                "permission", {"principal": "session", "tool_name": "edit_file"}
            )
        )

    for cancel in (False, True):
        worker = threading.Thread(target=request)
        worker.start()
        try:
            requested = events.get(timeout=2)
            assert requested.type == "interaction.requested"
            waiting = events.get(timeout=2)
            assert waiting.type == "plan.updated"
            assert (
                waiting.payload["plan"]["steps"][0]["wait_reason"]["kind"]
                == "permission"
            )
            if cancel:
                broker.cancel_pending()
            else:
                assert broker.resolve(requested.payload["request_id"], "allow_once")
            assert events.get(timeout=2).type == "interaction.resolved"
            resolved = events.get(timeout=2)
            assert "wait_reason" not in resolved.payload["plan"]["steps"][0]
            reconnect = project_plan(
                session.plan_manager.snapshot(),
                broker.snapshot(),
                owner_session_id="session",
            )
            assert "wait_reason" not in reconnect["steps"][0]
        finally:
            broker.cancel_pending()
            worker.join(timeout=2)
        assert not worker.is_alive()
    assert answers == ["allow_once", "deny"]
    broker.close()
    publisher.close()


def test_concurrent_approval_requests_keep_root_and_child_waits_separate(tmp_path):
    session = Session.create("goal", tmp_path, session_id="session")
    session.plan_manager.create_plan("goal", ["first"])
    session.plan_manager.update_step("step_1", "in_progress")
    publisher = EventPublisher(project_id="p", session_id="session")
    broker = InteractionBroker(publisher)
    bind_plan_events(publisher, session, broker)
    _, events = publisher.subscribe()
    answers = []
    workers = [
        threading.Thread(
            target=lambda principal=principal: answers.append(
                broker.request("permission", {"principal": principal})
            )
        )
        for principal in ("session", "session", "child-task")
    ]
    try:
        for worker in workers:
            worker.start()
        requested = []
        while len(requested) < 3:
            event = events.get(timeout=2)
            if event.type == "interaction.requested":
                requested.append(event)
        root_ids = {
            event.payload["request_id"]
            for event in requested
            if event.payload["principal"] == "session"
        }
        view = project_plan(
            session.plan_manager.snapshot(),
            broker.snapshot(),
            owner_session_id="session",
        )
        assert set(view["steps"][0]["wait_reason"]["request_ids"]) == root_ids
        assert len(view["wait_reason"]["request_ids"]) == 1
        assert view["wait_reason"]["kind"] == "permission"
        resolved = next(iter(root_ids))
        assert broker.resolve(resolved, "allow_once")
        view = project_plan(
            session.plan_manager.snapshot(),
            broker.snapshot(),
            owner_session_id="session",
        )
        assert set(view["steps"][0]["wait_reason"]["request_ids"]) == root_ids - {
            resolved
        }
        broker.cancel_pending()
        view = project_plan(
            session.plan_manager.snapshot(),
            broker.snapshot(),
            owner_session_id="session",
        )
        assert "wait_reason" not in view and "wait_reason" not in view["steps"][0]
        assert view["steps"][0]["status"] == "in_progress"
    finally:
        broker.close()
        for worker in workers:
            worker.join(timeout=2)
        publisher.close()
    assert not any(worker.is_alive() for worker in workers)
    assert sorted(answers) == ["allow_once", "deny", "deny"]


def test_plan_projection_publishes_after_root_lifecycle_and_reset(
    tmp_path, monkeypatch
):
    session = Session.create("goal", tmp_path)
    session.plan_manager.create_plan("goal", ["first"])
    publisher = EventPublisher(project_id="p", session_id=session.session_id)
    bind_plan_events(publisher, session, SimpleNamespace(snapshot=list))
    monkeypatch.setattr(
        "wright.application.planning.projection.time.time", lambda: 100.0
    )
    publisher.publish("session.status_changed", {"status": "model_turn_started"})
    assert publisher.retained_events()[-1].type == "plan.updated"
    session.plan_manager.reset()
    publisher.publish(
        "session.status_changed",
        {"status": "model_turn_started", "agent_depth": 1, "agent_task_id": "child"},
    )
    assert publisher.retained_events()[-1].type == "session.status_changed"
    publisher.publish("session.status_changed", {"status": "model_turn_started"})
    assert publisher.retained_events()[-1].payload["plan"]["steps"] == []
    publisher.publish("turn.completed", {})
    assert publisher.retained_events()[-1].payload["plan"]["observed_at"] == 100.0
    publisher.close()


@pytest.mark.parametrize(
    ("mode", "inside", "outside"),
    [
        ("default", ["allow", "ask", "ask"], ["ask", "ask", "ask"]),
        ("acceptEdits", ["allow", "allow", "ask"], ["ask", "ask", "ask"]),
        ("bypass", ["allow", "allow", "allow"], ["ask", "ask", "ask"]),
        ("plan", ["allow", "deny", "deny"], ["ask", "deny", "deny"]),
    ],
)
@pytest.mark.parametrize("interaction", ["agent", "ask", "plan"])
def test_permission_summary_defaults_match_actual_policy(
    mode, inside, outside, interaction
):
    policy = PermissionPolicy(PermissionSettings(mode=mode))
    summary = policy.summarize(roots=["/project"], interaction_mode=interaction)
    if interaction in {"ask", "plan"}:
        inside = ["allow", "deny", "deny"]
        outside = [
            "ask",
            "deny",
            "deny",
        ]
    for index, category in enumerate(summary):
        operation = category["operation"]
        for scope, expected in (
            ("in_scope", inside[index]),
            ("outside_scope", outside[index]),
        ):
            actual = policy.evaluate(
                ToolAccess(frozenset({operation})),
                tool_name="execute_command" if operation == "shell" else "read_file",
                subject="target",
                in_scope=scope == "in_scope",
                interaction_mode=interaction,
            )
            assert category["defaults"][scope]["decision"] == actual[0] == expected


def test_summary_exposes_conditions_and_bypass_does_not_hide_deny_or_ask():
    settings = PermissionSettings(
        mode="bypass",
        deny=[PermissionRule.parse("execute_command(rm *)", effect="deny")],
        ask=[PermissionRule.parse("execute_command(git push*)", effect="ask")],
        allow=[
            PermissionRule.file_grant("edit_file", "/project", "*.py", ("file_read", "file_write"))
        ],
    )
    policy = PermissionPolicy(settings)
    summary = {item["operation"]: item for item in policy.summarize(roots=["/project"])}
    shell = summary["shell"]
    assert shell["defaults"]["in_scope"]["decision"] == "allow"
    assert [item["effect"] for item in shell["rules"]] == ["deny", "ask"]
    assert all(item["conditional"] for item in shell["rules"])
    for command, expected in (
        ("rm file", "deny"),
        ("git push origin main", "ask"),
        ("echo ready", "allow"),
    ):
        assert (
            policy.evaluate(
                ToolAccess(frozenset({"shell"})),
                tool_name="execute_command",
                subject=command,
                in_scope=True,
            )[0]
            == expected
        )
    assert "protected_permission_files" in summary["file_write"]["constraints"]
    assert "shell_system_sandbox" in shell["constraints"]
    assert summary["file_read"]["rules"][0]["conditional"]
    rule = summary["file_write"]["rules"][0]
    assert rule["conditional"] and rule["rule"]["root"] == "/project"
    default = PermissionPolicy(PermissionSettings(allow=settings.allow))
    for path, expected in (
        ("/project/main.py", "allow"),
        ("/other/main.py", "ask"),
        ("/project/file.txt", "ask"),
    ):
        decision = default.evaluate(
            ToolAccess(frozenset({"file_read", "file_write"})),
            tool_name="edit_file",
            subject=path,
            in_scope=path.startswith("/project/"),
            context=MatchContext(files=((path, "file_read"), (path, "file_write"))),
        )
        assert decision[0] == expected


def test_grants_summary_uses_fresh_persisted_scope(tmp_path, monkeypatch):
    from wright.application.tool_execution.permissions import PermissionService
    from wright.domain.model.session import Session
    from wright.domain.policy.permission.types import AuthorizationChange
    from wright.infrastructure.config.permission_store import FilePermissionRepository
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    root, granted = tmp_path / "root", tmp_path / "granted"
    session = Session.create("scopes", root)
    permissions = PermissionService(FilePermissionRepository(root))
    rule = PermissionRule("*", kind="directory", root=str(granted), recursive=True, operations=("file_read",))
    permissions.commit(AuthorizationChange(project_rules=(rule.to_persistent(),)), session)
    summary = list_grants(session, permissions)
    assert all(item["directories"] == [str(root.absolute()), str(granted.absolute())] for item in summary["effective_policy"])
    assert summary["grants"][0]["lifetime"] == "project"


@pytest.mark.parametrize(
    ("path", "name", "parent"),
    [
        ("/project/file.txt", "file.txt", "/project"),
        ("C:\\project\\file.txt", "file.txt", "C:\\project"),
        (
            "\\\\server\\share\\folder\\file.txt",
            "file.txt",
            "\\\\server\\share\\folder",
        ),
    ],
)
def test_execution_paths_keep_environment_and_native_components(path, name, parent):
    resolved = ExecutionPath("environment", path)
    assert resolved.name == name
    assert resolved.parent == ExecutionPath("environment", parent)


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypass", "plan"])
@pytest.mark.parametrize("interaction", ["agent", "ask", "plan"])
@pytest.mark.parametrize(
    ("operation", "tool"),
    [
        ("file_read", read_file_tool),
        ("file_write", edit_file_tool),
        ("shell", execute_command_tool),
    ],
)
def test_summary_agrees_with_real_resolver_scopes_and_protected_paths(
    tmp_path, monkeypatch, mode, interaction, operation, tool
):
    root, extra, outside = (tmp_path / name for name in ("root", "extra", "outside"))
    for directory in (root, extra, outside):
        directory.mkdir()
    protected = root / "permissions.json"
    monkeypatch.setenv("WRIGHT_PERMISSION_CONFIG", str(protected))
    backend = LocalExecutionBackend(root, lambda: root)
    scope = AccessScope(root, (extra,), (protected,))
    policy = PermissionPolicy(PermissionSettings(mode=mode))
    summary = next(
        item
        for item in policy.summarize(
            roots=[str(root), str(extra)], interaction_mode=interaction
        )
        if item["operation"] == operation
    )
    prompts = []

    def approve(request):
        prompts.append(request.prompt)
        return "allow_once"

    resolver = PermissionResolver(policy, approve)
    subject = PermissionSubject(tool.name, False, tool.describe_access, lambda _: None)

    def resolve(path):
        prompts.clear()
        arguments = (
            {"command": "echo done"}
            if operation == "shell"
            else {"file": str(path), "old_text": "a", "new_text": "b"}
        )
        return resolver.resolve(
            ToolCall(tool.name, arguments, "call"),
            subject,
            backend=backend,
            scope=scope,
            identity=InvocationIdentity("session", call_id="call"),
            interaction_mode=interaction,
        )

    for directory, key in (
        (root, "in_scope"),
        (extra, "in_scope"),
        (outside, "outside_scope"),
    ):
        result = resolve(directory / "file.txt")
        expected = summary["defaults"]["in_scope" if operation == "shell" else key]["decision"]
        assert result.decision == ("allow" if expected == "ask" else expected)
        assert bool(prompts) == (expected == "ask")
        assert (result.grant is not None) == (expected != "deny")
    if operation != "shell":
        assert "protected_permission_files" in summary["constraints"]
        blocked = resolve(protected)
        assert blocked.decision == "deny" and blocked.source == "protected_path"
        assert blocked.grant is None and not prompts


def test_scripted_web_session_projects_diff_wait_policy_and_history(
    tmp_path, monkeypatch
):
    """Exercise real tools, mailbox persistence, HTTP and reconnect without a model service."""
    import argparse
    import queue
    import time

    from fastapi.testclient import TestClient

    from tests.responses import response
    from wright.application.composition import runtime as assembly
    from wright.interfaces.web.auth import BootstrapAuth
    from wright.interfaces.web.runtime_manager import RuntimeManager
    from wright.interfaces.web.server import create_app

    script = iter(
        [
            ("create_plan", {"objective": "edit file", "steps": ["apply edit"]}),
            ("update_plan", {"step_id": "step_1", "status": "in_progress"}),
            ("read_file", {"file": "file.txt"}),
            (
                "edit_file",
                {"file": "file.txt", "old_text": "old", "new_text": "new\r\nextra"},
            ),
            ("update_plan", {"step_id": "step_1", "status": "completed"}),
        ]
    )

    class Model:
        model = "offline"
        transport_name = "chat"
        context_limit = 128000

        def __call__(self, *_args, **_kwargs):
            call = next(script, None)
            yield (
                response(calls=[{"name": call[0], "arguments": call[1]}])
                if call
                else response(content="done")
            )

    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_MODEL", "offline")
    monkeypatch.setattr(assembly, "load_env", lambda: None)
    monkeypatch.setattr(assembly, "LLMClient", lambda **_: Model())
    monkeypatch.setattr(assembly, "load_mcp_configs", lambda _: [])
    monkeypatch.setattr(assembly, "optional_knowledge_tools", list)
    monkeypatch.setattr(
        assembly,
        "load_lifecycle_manager",
        lambda *a, **k: SimpleNamespace(emit=lambda *a, **k: None),
    )
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "file.txt"
    path.write_bytes(b"first\r\nold\r\nlast")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_bytes(b"<main>Wright</main>")
    manager = RuntimeManager(root, base_args=argparse.Namespace())
    client = TestClient(create_app(manager, BootstrapAuth("secret"), static_dir=static))
    headers = {"origin": "http://testserver"}
    try:
        assert (
            client.post(
                "/api/v1/auth/exchange", json={"token": "secret"}, headers=headers
            ).status_code
            == 200
        )
        opened = client.post(
            "/api/v1/sessions", json={"environment": "local"}, headers=headers
        )
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["session"]["session_id"]
        handle = manager.get(session_id)
        _, inbox = handle.publisher.subscribe()
        submitted = client.post(
            f"/api/v1/sessions/{session_id}/turns",
            json={"prompt": "edit file", "command_id": "script-turn"},
            headers=headers,
        )
        assert submitted.status_code == 200, submitted.text

        def until(event_type):
            events = []
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    item = inbox.get(timeout=0.5)
                except queue.Empty:
                    continue
                events.append(item)
                if item.type == event_type:
                    return events
                assert item.type not in {"turn.failed", "turn.cancelled"}, (
                    item.to_dict()
                )
            pytest.fail(
                f"Missing {event_type}; events: {[item.to_dict() for item in events]}"
            )

        requested = until("interaction.requested")[-1]
        assert requested.payload["tool_name"] == "edit_file"
        assert "allow_session_rule" in {
            choice["id"] for choice in requested.payload["choices"]
        }
        waiting = until("plan.updated")[-1].payload["plan"]
        assert waiting["steps"][0]["wait_reason"]["kind"] == "permission"
        assert waiting["steps"][0]["status"] == "in_progress"
        records = handle.runtime.autonomy_store.list_interactions(
            f"session:{session_id}"
        )
        assert records[0]["status"] == "pending"
        grants = client.get(f"/api/v1/sessions/{session_id}/grants").json()
        assert {item["operation"] for item in grants["effective_policy"]} == {
            "file_read",
            "file_write",
            "shell",
        }

        with client.websocket_connect(
            f"/api/v1/sessions/{session_id}/stream?stream_id=fresh", headers=headers
        ) as socket:
            reconnect = socket.receive_json()["snapshot"]
            assert reconnect["plan"]["steps"][0]["wait_reason"]["request_ids"] == [
                requested.payload["request_id"]
            ]
            socket.send_json(
                {
                    "type": "interaction.respond",
                    "command_id": "approve-edit",
                    "request_id": requested.payload["request_id"],
                    "answer": "allow_session_rule",
                }
            )
            finished = until("turn.completed")

        edit_event = next(
            item
            for item in finished
            if item.type == "tool.finished" and item.payload["name"] == "edit_file"
        )
        assert edit_event.payload["ok"], edit_event.payload
        assert any(item.type == "session.policy_updated" for item in finished)
        updated_grants = client.get(f"/api/v1/sessions/{session_id}/grants").json()
        assert any(rule["source"] == "session" for rule in updated_grants["grants"])
        write_policy = next(
            item
            for item in updated_grants["effective_policy"]
            if item["operation"] == "file_write"
        )
        assert any(
            rule["scope"] == "session" and rule["conditional"]
            for rule in write_policy["rules"]
        )
        data = edit_event.payload["data"]
        assert data["additions"] == 2 and data["deletions"] == 1
        assert "@@ -1,3 +1,4 @@" in data["diff"]
        assert path.read_bytes() == b"first\r\nnew\r\nextra\r\nlast"
        current = client.get(f"/api/v1/sessions/{session_id}/snapshot").json()
        completed = current["plan"]["steps"][0]
        assert completed["ended_at"] >= completed["started_at"]
        assert completed["elapsed_ms"] >= 0 and "wait_reason" not in completed
        assert current["pending_interactions"] == []
        timeline_edit = next(
            item for item in current["timeline"] if item.get("name") == "edit_file"
        )
        assert timeline_edit["result"]["data"]["diff"] == data["diff"]
        with client.websocket_connect(
            f"/api/v1/sessions/{session_id}/stream?stream_id=fresh", headers=headers
        ) as socket:
            reconnect = socket.receive_json()["snapshot"]
            assert reconnect["pending_interactions"] == []
            assert "wait_reason" not in reconnect["plan"]["steps"][0]
        records = handle.runtime.autonomy_store.list_interactions(
            f"session:{session_id}"
        )
        assert records[0]["status"] == "approved"
        assert records[0]["resolution"]["choice"] == "allow_session_rule"
        manager.close(session_id)
        history = client.get(f"/api/v1/sessions/{session_id}/preview").json()
        assert history["plan"]["steps"][0]["elapsed_ms"] == completed["elapsed_ms"]
        assert not history["session"]["active"]
        historic_edit = next(
            item for item in history["timeline"] if item.get("name") == "edit_file"
        )
        assert historic_edit["result"]["data"]["diff"] == data["diff"]
    finally:
        manager.shutdown()
