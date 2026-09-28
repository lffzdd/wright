"""Capabilities behind the workspace console.

These tests use temporary directories and stand-in models. They do not edit
the user's real project or permission file.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from wright.application.autonomy.service import AutonomyService
from wright.application.execution.identity import bind_identity
from wright.application.memory.memory_service import MemoryService
from wright.application.workspace.catalog import WorkspaceCatalog
from wright.application.workspace.context_usage import classify_context
from wright.application.workspace.documents import render_documents, store_document
from wright.application.workspace.files import (
    PathRejected,
    list_directory,
    resolve_inside,
)
from wright.application.workspace.grants import (
    add_session_rule,
    change_directory,
    list_grants,
    revoke_persistent_rule,
    revoke_session_rule,
)
from wright.application.workspace.journal import SessionChangeJournal
from wright.application.workspace.memory_view import (
    create_semantic,
    delete_semantic,
    project_memory,
)
from wright.application.workspace.modes import (
    ASK_OPERATIONS,
    PLAN_OPERATIONS,
    tool_visible,
)
from wright.application.workspace.rules import delete_rule, list_rules, save_rule
from wright.application.workspace.schedule_api import (
    create_schedule,
    delete_schedule,
    list_project_schedules,
    pause_schedule,
    project_action,
    resume_schedule,
)
from wright.application.workspace.search import (
    search_content,
    search_symbols,
    search_tasks,
)
from wright.application.workspace.timeline import project_subagents, project_timeline
from wright.core.paths import project_id, task_db_path
from wright.domain.model.agent.control import AgentControlPlane
from wright.domain.policy.permission.resolver import PermissionPolicy
from wright.domain.policy.permission.settings import PermissionSettings
from wright.domain.policy.permission.types import ToolAccess
from wright.infrastructure.persistence.autonomy_store import AutonomyStore
from wright.infrastructure.persistence.memory import EpisodeStore, SemanticMemoryStore
from wright.interfaces.web.auth import BootstrapAuth
from wright.interfaces.web.runtime_manager import RuntimeManager
from wright.interfaces.web.server import create_app


class _Message:
    def __init__(self, role, content, source="history"):
        self.message = {"role": role, "content": content}
        self.source = source


def test_context_categories_are_exclusive_estimates():
    view = SimpleNamespace(
        entries=(
            _Message("system", "You are helpful.\n<CORE_MEMORY>anchor</CORE_MEMORY>"),
            _Message("user", "<skill-catalog>review</skill-catalog>", "transient"),
            _Message("user", "wright-semantic-recall: note"),
            _Message("assistant", "hello"),
            _Message("tool", "file contents"),
        ),
        tool_schema_tokens=4,
        output_reserve_tokens=2,
    )
    report = classify_context(view)
    tokens = [item["tokens"] for item in report["categories"]]
    assert report["kind"] == "tokenizer_estimate"
    assert report["exact"] is False
    assert report["system_prompt_contains_core_memory"] is True
    assert sum(tokens) == report["total"]
    by_id = {item["id"]: item["tokens"] for item in report["categories"]}
    assert by_id["tool_schemas"] == 4
    assert by_id["output_reserve"] == 2
    assert by_id["rules"] > 0
    assert by_id["memory"] > 0
    assert by_id["system_prompt"] > 0


def test_paths_reject_escape_and_symlink(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside)
    with pytest.raises(PathRejected):
        resolve_inside(root, "../outside/secret.txt")
    with pytest.raises(PathRejected):
        resolve_inside(root, "/etc/passwd")
    with pytest.raises(PathRejected):
        resolve_inside(root, "link/secret.txt")
    listing = list_directory(root, "")
    assert all(item["name"] != "secret.txt" for item in listing["entries"])


def test_symbol_search_is_not_a_text_search(tmp_path: Path):
    (tmp_path / "widget.py").write_text("def AlphaWidget():\n    return 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("AlphaWidget is mentioned here\n", encoding="utf-8")
    symbols = search_symbols(tmp_path, "AlphaWidget")
    content = search_content(tmp_path, "AlphaWidget")
    assert [item["path"] for item in symbols] == ["widget.py"]
    assert symbols[0]["kind"] == "symbol"
    assert "notes.md" in [item["path"] for item in content]
    assert all(item["kind"] == "content" for item in content)


def test_task_search_stays_inside_the_supplied_sessions():
    found = search_tasks(
        [
            {"session_id": "a", "user_goal": "fix parser", "status": "open", "project_root": "/a"},
            {"session_id": "b", "user_goal": "other", "status": "open", "project_root": "/b"},
        ],
        "parser",
    )
    assert [item["session_id"] for item in found] == ["a"]


def test_catalog_unregister_keeps_files_and_selection_is_not_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "keep.txt").write_text("stay", encoding="utf-8")
    catalog = WorkspaceCatalog(tmp_path / "home" / "workspaces.json")
    registered = catalog.register(first)
    catalog.register(second)
    selected = catalog.select(project_id(second))
    assert selected["selected_project_id"] == project_id(second)
    removed = catalog.unregister(registered["project_id"])
    assert removed["deleted_files"] is False
    assert (first / "keep.txt").read_text(encoding="utf-8") == "stay"
    assert Path.cwd() != second


def test_change_attribution_conflict_and_batch_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    tracked = root / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    tracked.write_text("dirty before task\n", encoding="utf-8")
    session = SimpleNamespace(session_id="s1", project_root=root, workspace_dir=root)
    journal = SessionChangeJournal(session)
    journal.ensure_baseline()
    preexisting = {item["path"]: item for item in journal.projection()["changes"]}
    assert preexisting["tracked.txt"]["state"] == "preexisting"
    blocked = journal.revert(["tracked.txt"], confirm=True)
    assert blocked["applied"] is False
    assert tracked.read_text(encoding="utf-8") == "dirty before task\n"

    created = root / "added.txt"
    created.write_text("task\n", encoding="utf-8")
    journal.note_text(created, before=None, after="task\n", existed=False, call_id="c1", tool_name="write_file")
    other = root / "other.txt"
    other.write_text("other\n", encoding="utf-8")
    journal.note_text(other, before=None, after="other\n", existed=False, call_id="c2", tool_name="write_file")
    created.write_text("external\n", encoding="utf-8")
    conflicted = journal.revert(["added.txt", "other.txt"], confirm=True)
    assert conflicted["applied"] is False
    assert other.read_text(encoding="utf-8") == "other\n"
    assert created.read_text(encoding="utf-8") == "external\n"

    created.write_text("task\n", encoding="utf-8")
    reviewed = journal.accept(["added.txt", "other.txt"], confirm=True)
    assert reviewed["applied"] is True
    assert created.read_text(encoding="utf-8") == "task\n"
    states = {item["path"]: item["state"] for item in journal.projection()["changes"]}
    assert states["added.txt"] == "accepted"
    assert states["other.txt"] == "accepted"

    fresh = root / "fresh.txt"
    fresh.write_text("fresh\n", encoding="utf-8")
    journal.note_text(fresh, before=None, after="fresh\n", existed=False, call_id="c4", tool_name="write_file")
    original_restore = journal._restore_before

    def fail_fresh(path: str) -> None:
        if path == "fresh.txt":
            raise OSError("disk")
        original_restore(path)

    journal._restore_before = fail_fresh
    failed = journal.revert(["other.txt", "fresh.txt"], confirm=True)
    assert failed["applied"] is False
    assert other.read_text(encoding="utf-8") == "other\n"
    assert fresh.read_text(encoding="utf-8") == "fresh\n"


def test_ask_ceiling_survives_bypass_and_grant_revoke_is_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    settings = PermissionSettings(mode="bypass")
    decision = PermissionPolicy(settings).evaluate(
        ToolAccess(frozenset({"file_write"})),
        tool_name="write_file",
        subject="a.txt",
        in_scope=True,
        interaction_mode="ask",
        permission_mode="bypass",
    )
    assert decision[0] == "deny"
    assert decision[3] == "permission.ask_denied"
    plan = PermissionPolicy(settings).evaluate(
        ToolAccess(frozenset({"plan_update"})),
        tool_name="update_plan",
        subject="plan",
        in_scope=True,
        interaction_mode="plan",
        permission_mode="bypass",
    )
    assert plan[0] != "deny"
    assert not tool_visible("write_file", SimpleNamespace(interaction_mode="ask", permission_mode="bypass"))
    assert tool_visible("update_plan", SimpleNamespace(interaction_mode="plan", permission_mode="bypass"))
    assert PermissionPolicy._ASK_OPERATIONS == ASK_OPERATIONS
    assert PermissionPolicy._PLAN_OPERATIONS == PLAN_OPERATIONS

    class Rules:
        def __init__(self):
            self.permission_rules = []

        def add_permission_rule(self, rule):
            self.permission_rules.append(rule)

        def remove_permission_rule(self, rule):
            before = len(self.permission_rules)
            self.permission_rules = [item for item in self.permission_rules if item != rule]
            return len(self.permission_rules) != before

    session = Rules()
    created = add_session_rule(session, "read_file")
    assert list_grants(session, settings)["session_rules"]
    with pytest.raises(Exception, match="confirmation"):
        revoke_session_rule(session, created["id"], confirm=False)
    assert revoke_session_rule(session, created["id"], confirm=True)["ok"] is True
    assert session.permission_rules == []

    path = tmp_path / "home" / "permission_settings.json"
    persistent = PermissionSettings.from_dict({"mode": "default", "permissions": {"allow": ["read_file"]}})
    listed = list_grants(SimpleNamespace(permission_rules=[], interaction_mode="agent"), persistent)
    rule_id = listed["persistent_rules"][0]["id"]
    revoked = revoke_persistent_rule(persistent, rule_id, confirm=True, path=path)
    assert revoked["scope"] == "persistent"
    assert persistent.allow == []


def test_directory_grant_can_be_added_and_removed(tmp_path: Path):
    extra = tmp_path / "extra"
    root = tmp_path / "root"
    extra.mkdir()
    root.mkdir()

    class Box:
        def __init__(self):
            self.workspace_dir = root
            self.directories: list[Path] = []

        def add_working_directory(self, directory: Path) -> Path:
            self.directories.append(directory)
            return directory

        def remove_working_directory(self, directory: Path) -> bool:
            before = len(self.directories)
            self.directories = [item for item in self.directories if item != directory]
            return len(self.directories) != before

    session = Box()
    settings = PermissionSettings()
    store = tmp_path / "permission_settings.json"
    added = change_directory(
        session, settings, str(extra), scope="session", confirm=True, action="add",
    )
    assert added["path"] == str(extra.resolve())
    assert session.directories == [extra.resolve()]
    with pytest.raises(Exception, match="confirmation"):
        change_directory(
            session, settings, str(extra), scope="session", confirm=False, action="remove",
        )
    removed = change_directory(
        session, settings, str(extra), scope="session", confirm=True, action="remove",
    )
    assert removed["ok"] is True
    assert session.directories == []
    persistent = change_directory(
        session,
        settings,
        str(extra),
        scope="persistent",
        confirm=True,
        action="add",
        store_path=store,
    )
    assert persistent["path"] in settings.additional_directories
    change_directory(
        session,
        settings,
        str(extra),
        scope="persistent",
        confirm=True,
        action="remove",
        store_path=store,
    )
    assert settings.additional_directories == []
    with pytest.raises(Exception, match="execution root"):
        change_directory(
            session, settings, str(root), scope="session", confirm=True, action="remove",
        )


def test_memory_and_rules_are_project_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    service = MemoryService(
        semantic_store=SemanticMemoryStore(tmp_path / "memory"),
        episode_store=EpisodeStore(tmp_path / "memory"),
    )
    created = create_semantic(
        service,
        bound_project_id=project_id(one),
        name="note",
        content="only one",
        description="scoped",
        type_="project",
        scope="project",
    )
    visible = project_memory(service, bound_project_id=project_id(one))
    hidden = project_memory(service, bound_project_id=project_id(two))
    assert any(item.get("id") == created["id"] for item in visible["semantic"])
    assert all(item.get("id") != created["id"] for item in hidden["semantic"])
    with pytest.raises(Exception, match="confirmation"):
        delete_semantic(service, created["id"], bound_project_id=project_id(one), confirm=False)
    assert delete_semantic(service, created["id"], bound_project_id=project_id(one), confirm=True)["deleted"]

    saved = save_rule(one, "review-notes", scope="project", description="Review notes", body="Be precise.")
    assert saved["id"] == "review-notes"
    assert any(item.get("id") == "review-notes" for item in list_rules(one)["rules"])
    assert all(item.get("id") != "review-notes" for item in list_rules(two)["rules"])
    with pytest.raises(Exception, match="confirmation"):
        delete_rule(one, "review-notes", scope="project", confirm=False)
    assert delete_rule(one, "review-notes", scope="project", confirm=True)["deleted"] is True


def test_schedule_survives_reopen_and_delete_keeps_history(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()

    class Closed:
        control_plane = AgentControlPlane()

        @staticmethod
        def get_command(_identifier: str):
            return None

    store = AutonomyStore(task_db_path(project), session_id="session-a", workspace_dir=project)
    runtime = SimpleNamespace(
        autonomy_store=store,
        session_state=Closed(),
        services=SimpleNamespace(autonomy_scheduler=None),
    )
    created = create_schedule(
        runtime,
        name="nightly",
        prompt="summarize",
        trigger={"type": "interval", "every_seconds": 3600},
    )
    store.close()
    listed = list_project_schedules(project)
    assert listed[0]["id"] == created["id"]
    assert listed[0]["status"] == "active"

    reopened = AutonomyStore(task_db_path(project), session_id="session-a", workspace_dir=project)
    runtime.autonomy_store = reopened
    paused = pause_schedule(runtime, created["id"])
    assert paused["status"] == "paused"
    resumed = resume_schedule(runtime, created["id"])
    assert resumed["status"] == "active"
    with pytest.raises(Exception, match="confirmation"):
        delete_schedule(runtime, created["id"], confirm=False)
    removed = delete_schedule(runtime, created["id"], confirm=True)
    assert removed["deleted"] is True
    reopened.close()

    history = AutonomyStore(task_db_path(project), session_id="session-a", workspace_dir=project)
    runtime.autonomy_store = history
    again = create_schedule(
        runtime,
        name="kept",
        prompt="run",
        trigger={"type": "once", "run_at": 10},
    )
    service = AutonomyService(history, bind_identity(runtime.session_state, history), None)
    service._store.materialize_due(now=10)
    retained = delete_schedule(runtime, again["id"], confirm=True)
    assert retained["deleted"] is False
    assert retained["retained_for_history"] is True
    assert history.get_automation(again["id"]).status == "cancelled"
    history.close()
    parked = AutonomyStore(task_db_path(project), session_id="session-a", workspace_dir=project)
    runtime.autonomy_store = parked
    later = create_schedule(
        runtime,
        name="after-restart",
        prompt="still there",
        trigger={"type": "interval", "every_seconds": 3600},
    )
    parked.close()
    paused_closed = project_action(project, later["id"], "pause")
    assert paused_closed["status"] == "paused"
    rows = {item["id"]: item for item in list_project_schedules(project)}
    assert rows[later["id"]]["status"] == "paused"


def test_timeline_uses_records_not_prose(tmp_path: Path):
    class Record:
        def __init__(self, identifier, role, text, source):
            self.id = identifier
            self.source = source
            self.message = {"role": role, "content": text}

    session = SimpleNamespace(
        message_records=[
            Record("m1", "user", "start", "user_input"),
            Record("m2", "assistant", "the sub-agent is completed", "model"),
        ],
        tool_executions={},
        control_plane=SimpleNamespace(tree=lambda _root: [{
            "id": "root",
            "parent_id": "",
            "status": "running",
            "children": [{
                "id": "child",
                "parent_id": "root",
                "status": "running",
                "task": "look around",
                "ended_at": None,
                "children": [],
            }],
        }]),
    )
    timeline = project_timeline(session, [{"request_id": "perm-1"}])
    assert [item["id"] for item in timeline] == ["m1", "m2", "perm-1"]
    assert timeline[-1]["kind"] == "approval"
    agents = project_subagents(session)
    assert agents == [{
        "task_id": "child",
        "parent_id": "root",
        "status": "running",
        "task": "look around",
        "ended_at": None,
    }]


def test_documents_inline_without_entering_the_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    session = SimpleNamespace(
        session_id="s1", project_root=project, workspace_dir=project, documents={},
    )
    record = store_document(session, "../escape.txt", b"hello file")
    session.documents[record["id"]] = record
    assert ".." not in record["storage_path"]
    assert not (project / "escape.txt").exists()
    rendered = render_documents(session, [record["id"]])
    assert "hello file" in rendered


def _client(tmp_path: Path, manager: RuntimeManager) -> TestClient:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<main>Wright</main>", encoding="utf-8")
    client = TestClient(create_app(manager, BootstrapAuth("secret"), static_dir=static))
    response = client.post(
        "/api/v1/auth/exchange",
        json={"token": "secret"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 200
    return client


def test_workspace_http_isolates_projects_and_preferences(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    launch = tmp_path / "launch"
    other = tmp_path / "other"
    launch.mkdir()
    other.mkdir()
    (other / "secret.txt").write_text("hidden", encoding="utf-8")
    (launch / "visible.txt").write_text("shown", encoding="utf-8")
    manager = RuntimeManager(launch, base_args=argparse.Namespace())
    client = _client(tmp_path, manager)
    try:
        listed = client.get("/api/v1/workspaces")
        assert listed.status_code == 200
        other_record = client.post(
            "/api/v1/workspaces",
            json={"path": str(other)},
            headers={"origin": "http://testserver"},
        )
        assert other_record.status_code == 200
        other_id = other_record.json()["project_id"]
        secret = client.get(f"/api/v1/workspaces/{other_id}/file", params={"path": "secret.txt"})
        assert secret.status_code == 200
        assert secret.json()["content"] == "hidden"
        escaped = client.get(
            f"/api/v1/workspaces/{project_id(launch)}/file",
            params={"path": "../other/secret.txt"},
        )
        assert escaped.status_code == 400
        selected = client.post(
            "/api/v1/workspaces/select",
            json={"project_id": other_id},
            headers={"origin": "http://testserver"},
        )
        assert selected.status_code == 200
        assert manager.project_root == launch.resolve()
        empty = client.put(
            "/api/v1/preferences",
            json={},
            headers={"origin": "http://testserver"},
        )
        assert empty.status_code == 400
        saved = client.put(
            "/api/v1/preferences",
            json={"interface_language": "zh-CN", "theme": "light", "inspector_open": False},
            headers={"origin": "http://testserver"},
        )
        assert saved.status_code == 200
        assert saved.json()["theme"] == "light"
        assert saved.json()["inspector_open"] is False
        assert saved.json()["interface_language"] == "zh-CN"
        commands = client.get("/api/v1/commands")
        assert commands.status_code == 200
        assert any(item["name"] == "/help" for item in commands.json())
        removed = client.delete(
            f"/api/v1/workspaces/{other_id}",
            headers={"origin": "http://testserver"},
        )
        assert removed.status_code == 200
        assert (other / "secret.txt").read_text(encoding="utf-8") == "hidden"
    finally:
        manager.shutdown()


def test_switching_workspace_does_not_retarget_a_running_root(tmp_path: Path, monkeypatch):
    from tests.responses import response
    from wright.application.composition import runtime as assembly

    class Model:
        model = "offline"
        transport_name = "chat"
        context_limit = 1000

        def __call__(self, *_args, **_kwargs):
            yield response(content="done")

    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_MODEL", "offline")
    monkeypatch.setattr(assembly, "load_env", lambda: None)
    monkeypatch.setattr(assembly, "LLMClient", lambda **_: Model())
    monkeypatch.setattr(assembly, "load_mcp_configs", lambda _: [])
    monkeypatch.setattr(assembly, "optional_knowledge_tools", list)
    monkeypatch.setattr(
        assembly, "load_lifecycle_manager", lambda *a, **k: SimpleNamespace(emit=lambda *a, **k: None),
    )
    launch = tmp_path / "launch"
    other = tmp_path / "other"
    launch.mkdir()
    other.mkdir()
    manager = RuntimeManager(launch, base_args=argparse.Namespace())
    client = _client(tmp_path, manager)
    try:
        created = client.post(
            "/api/v1/workspaces",
            json={"path": str(other)},
            headers={"origin": "http://testserver"},
        )
        other_id = created.json()["project_id"]
        opened = client.post(
            f"/api/v1/workspaces/{other_id}/sessions",
            headers={"origin": "http://testserver"},
        )
        assert opened.status_code == 200
        session_id = opened.json()["session"]["session_id"]
        workspace = client.get(f"/api/v1/sessions/{session_id}/workspace")
        assert Path(workspace.json()["execution_root"]) == other.resolve()
        client.post(
            "/api/v1/workspaces/select",
            json={"project_id": project_id(launch)},
            headers={"origin": "http://testserver"},
        )
        again = client.get(f"/api/v1/sessions/{session_id}/workspace")
        assert Path(again.json()["execution_root"]) == other.resolve()
        policy = client.post(
            f"/api/v1/sessions/{session_id}/policy",
            json={"interaction_mode": "ask", "permission_mode": "bypass"},
            headers={"origin": "http://testserver"},
        )
        assert policy.status_code == 200
        assert policy.json()["interaction_mode"] == "ask"
        assert policy.json()["effective"] == "next permission resolution"
        snapshot = client.get(f"/api/v1/sessions/{session_id}/snapshot")
        body = snapshot.json()
        assert body["context_breakdown"]["kind"] == "tokenizer_estimate"
        assert body["timeline"] == []
        assert "stream_id" in body
    finally:
        manager.shutdown()


def test_rename_of_a_clean_file_reverts_without_touching_other_work(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "kept.txt").write_text("leave me\n", encoding="utf-8")
    (root / "old.txt").write_text("same\n", encoding="utf-8")
    subprocess.run(["git", "add", "kept.txt", "old.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    (root / "kept.txt").write_text("edited outside the task\n", encoding="utf-8")
    session = SimpleNamespace(session_id="s-rename", project_root=root, workspace_dir=root)
    journal = SessionChangeJournal(session)
    before = journal.capture()
    (root / "old.txt").rename(root / "new.txt")
    journal.commit_capture(before, call_id="mv1", tool_name="execute_command")
    changes = {item["path"]: item for item in journal.projection()["changes"]}
    assert changes["old.txt"]["display_kind"] == "rename"
    assert changes["old.txt"]["rename_with"] == "new.txt"
    assert changes["new.txt"]["rename_with"] == "old.txt"
    blocked = journal.revert(["kept.txt", "old.txt", "new.txt"], confirm=True)
    assert blocked["applied"] is False
    assert (root / "kept.txt").read_text(encoding="utf-8") == "edited outside the task\n"
    assert not (root / "old.txt").exists()
    restored = journal.revert(["old.txt", "new.txt"], confirm=True)
    assert restored["applied"] is True
    assert (root / "old.txt").read_text(encoding="utf-8") == "same\n"
    assert not (root / "new.txt").exists()
    assert (root / "kept.txt").read_text(encoding="utf-8") == "edited outside the task\n"
