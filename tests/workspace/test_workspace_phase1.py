"""Capabilities behind the workspace console.

These tests use temporary directories and stand-in models. They do not edit
the user's real project or permission file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from wright.application.agent.context import (
    MAX_OUTPUT_RESERVE,
    ContextBuilder,
    ContextCompactor,
    default_output_reserve,
)
from wright.application.composition.agent_factory import create_agent
from wright.application.composition.stores import open_autonomy_store, skill_store
from wright.application.memory.service import MemoryService
from wright.application.permission.resources import resource_change
from wright.application.permission.service import PermissionService
from wright.application.workspace.catalog import WorkspaceCatalog
from wright.application.workspace.context_usage import (
    MAX_ITEMS,
    calibrate,
    classify_context,
    context_detail,
    history_entry,
)
from wright.application.workspace.documents import render_documents, store_document
from wright.application.workspace.files import (
    PathRejected,
    list_directory,
    resolve_inside,
)
from wright.application.workspace.grants import list_grants
from wright.application.workspace.journal import SessionChangeJournal
from wright.application.workspace.memory_view import (
    MemoryViewError,
    create_fact,
    forget,
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
from wright.application.workspace.timeline import (
    project_accessed_files,
    project_subagents,
    project_timeline,
)
from wright.core.paths import project_id, task_db_path
from wright.domain.model.agent.control import AgentControlPlane
from wright.domain.model.session import Session, SessionAuthorization
from wright.domain.model.session.conversation import MessageRecord
from wright.domain.policy.permission.resolver import PermissionPolicy
from wright.domain.policy.permission.settings import PermissionSettings
from wright.domain.policy.permission.types import ToolAccess
from wright.infrastructure.config.permission_store import FilePermissionRepository
from wright.infrastructure.persistence.autonomy_store import AutonomyStore
from wright.infrastructure.persistence.memory import SqliteMemoryStore
from wright.infrastructure.runtime import LocalExecutionBackend
from wright.interfaces.web.auth import BootstrapAuth
from wright.interfaces.web.runtime_manager import RuntimeManager
from wright.interfaces.web.server import create_app


class _Message:
    def __init__(self, role, content, source="history", **extra):
        self.message = {"role": role, "content": content, **extra}
        self.source = source


def _tool_result(call_id, data, **extra):
    return _Message("tool", json.dumps({"ok": True, "data": data, **extra}), tool_call_id=call_id)


def _call(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_context_categories_are_exclusive_estimates():
    view = SimpleNamespace(
        entries=(
            _Message("system", "<MEMORY_CONTEXT>\nanchor\n</MEMORY_CONTEXT>\nYou are helpful."),
            _Message("user", "<skill-catalog>\n- review: check\n</skill-catalog>", "transient"),
            _Message("user", "wright-fact-recall: note"),
            _Message("user", "fix the bug"),
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
    assert report["system_prompt_contains_memory"] is True
    assert sum(tokens) == report["total"]
    assert report["used"] == report["total"] - 2
    by_id = {item["id"]: item for item in report["categories"]}
    assert by_id["tool_schemas"]["tokens"] == 4
    assert by_id["output_reserve"]["group"] == "reserve"
    assert by_id["skills"]["items"][0]["count"] == 1
    assert by_id["user"]["items"][0]["label"] == "fix the bug"
    assert by_id["assistant"]["tokens"] > 0
    # The resident memory block moves out of the system prompt into memory;
    # recall changes every turn, so it counts with the conversation.
    assert [item["key"] for item in by_id["memory"]["items"]] == ["resident_memory"]
    assert by_id["recalled_memory"]["group"] == "conversation"
    assert [item["key"] for item in by_id["recalled_memory"]["items"]] == ["recalled_facts"]
    assert by_id["system_prompt"]["tokens"] > 0
    assert by_id["system_prompt"]["group"] == "fixed"
    assert by_id["system_prompt"]["items"][0]["label"] == "You are helpful."


def test_context_items_name_each_tool_call_and_mcp_server():
    view = SimpleNamespace(
        entries=(
            _Message("assistant", "", tool_calls=[
                _call("c1", "read_file", path="src/app.py"),
                _call("c2", "shell", command="pytest -q\n--tb=short"),
                _call("c3", "load_skill", skill="release-check"),
            ]),
            _tool_result("c1", "x" * 400, folded=True),
            _tool_result("c2", "y" * 4000),
            _tool_result("c3", "steps", retention="instruction", retention_key="skill:release-check"),
        ),
        tools=(
            {"name": "read_file", "description": "Read", "parameters": {}},
            {"name": "mcp__demo__clock", "description": "Time", "parameters": {}},
            {"name": "mcp__demo__echo", "description": "Echo", "parameters": {}},
        ),
        tool_schema_tokens=30,
        output_reserve_tokens=0,
        folded_tokens=120,
    )
    report = classify_context(view)
    by_id = {item["id"]: item for item in report["categories"]}
    outputs = by_id["tool_output"]["items"]
    assert [(item["label"], item["detail"]) for item in outputs] == [("shell", "pytest -q"), ("read_file", "src/app.py")]
    assert outputs[1]["folded"] is True and outputs[1]["call_id"] == "c1"
    assert report["folded"] == {"count": 1, "tokens": 120}
    assert by_id["loaded_skills"]["items"][0]["label"] == "release-check"
    assert by_id["loaded_skills"]["items"][0]["loaded"] is True
    # MCP schemas are their own category; built-in tools keep the rest.
    servers = by_id["mcp_schemas"]["items"]
    assert [(item["label"], item["mcp"], item["count"]) for item in servers] == [("demo", "demo", 2)]
    assert [item["label"] for item in by_id["tool_schemas"]["items"]] == ["read_file"]
    assert by_id["tool_schemas"]["tokens"] + by_id["mcp_schemas"]["tokens"] == 30
    # A reply with no text is only tool-call arguments.
    assert by_id["assistant"]["tokens"] == 0
    assert {item["label"] for item in by_id["tool_calls"]["items"]} == {"read_file", "shell", "load_skill"}
    assert by_id["tool_calls"]["count"] == 3


def test_context_splits_replies_and_routes_tool_results():
    view = SimpleNamespace(
        entries=(
            _Message("user", "look", parts=[{"type": "text", "text": "look"}, {"type": "image"}, {"type": "image"}]),
            _Message("assistant", "Reading now.", reasoning_content="r" * 400, tool_calls=[
                _call("c1", "write", path="a.py", content="z" * 800),
                _call("c2", "mcp__demo__clock"),
                _call("c3", "spawn_agent", task="review"),
                _call("c4", "read", path="a.py"),
                _call("c5", "read", path="b.py"),
            ]),
            _tool_result("c1", "ok"),
            _tool_result("c2", "noon"),
            _tool_result("c3", "done " * 50),
            _tool_result("c4", "x" * 400),
            _tool_result("c5", "y" * 200),
        ),
        tool_schema_tokens=0,
        output_reserve_tokens=0,
    )
    report = classify_context(view)
    by_id = {item["id"]: item for item in report["categories"]}
    assert sum(item["tokens"] for item in report["categories"]) == report["total"]
    assert by_id["attachments"]["tokens"] == 2048
    assert by_id["attachments"]["items"][0]["count"] == 2
    assert by_id["reasoning"]["tokens"] > 0
    assert by_id["tool_calls"]["tokens"] > by_id["assistant"]["tokens"] > 0
    assert by_id["assistant"]["items"][0]["label"] == "Reading now."
    assert [item["label"] for item in by_id["mcp_output"]["items"]] == ["mcp__demo__clock"]
    assert [item["label"] for item in by_id["subagent_output"]["items"]] == ["spawn_agent"]
    assert {item["label"] for item in by_id["tool_output"]["items"]} == {"write", "read"}
    reads = next(item for item in by_id["tool_output"]["by_tool"] if item["label"] == "read")
    assert reads["count"] == 2
    assert by_id["tool_output"]["by_tool"][0]["label"] == "read"


def test_context_reports_where_folding_starts():
    view = SimpleNamespace(entries=(), tool_schema_tokens=0, output_reserve_tokens=1000, context_limit=10_000)
    assert classify_context(view, watermark=0.75)["fold_at"] == 6500
    assert "fold_at" not in classify_context(view)


def test_calibration_anchors_the_total_on_the_last_provider_count():
    report = calibrate({"used": 1200}, [{"estimated": 900, "actual": 1000}, {"estimated": 1100, "actual": 1300}])
    # 1300 counted for the last request, plus 100 estimated since.
    assert report["calibrated_used"] == 1400
    assert report["anchor"] == {"actual": 1300, "estimated": 1100}
    assert len(report["history"]) == 2
    unanchored = calibrate({"used": 1200}, [{"estimated": 900}])
    assert "calibrated_used" not in unanchored
    assert unanchored["history"] == [{"estimated": 900}]
    assert calibrate({"used": 5}, [{"estimated": 900, "actual": 100}])["calibrated_used"] == 0
    assert history_entry({"used": 50}, 70) == {"estimated": 50, "actual": 70}
    assert history_entry({"used": 50}, None) == {"estimated": 50}


def test_reply_reserve_is_capped_for_large_windows():
    assert default_output_reserve(8_000) == 1_000
    assert default_output_reserve(200_000) == 25_000
    assert default_output_reserve(1_000_000) == MAX_OUTPUT_RESERVE
    assert default_output_reserve(None) == 0


def test_context_lists_only_the_largest_items():
    entries = tuple(_Message("user", f"message {index} " + "z" * index * 10) for index in range(MAX_ITEMS + 4))
    report = classify_context(SimpleNamespace(entries=entries, tool_schema_tokens=0, output_reserve_tokens=0))
    user = next(item for item in report["categories"] if item["id"] == "user")
    assert user["count"] == MAX_ITEMS + 4
    assert len(user["items"]) == MAX_ITEMS
    assert user["items"][0]["label"].startswith(f"message {MAX_ITEMS + 3}")


def _turn_view():
    return SimpleNamespace(
        entries=(
            _Message("system", "You are helpful.\n\nAlways available: read.\n\nFollow the user's language."),
            _Message("user", "first task", "user_input"),
            _Message("assistant", "On it.", reasoning_content="think " * 40, tool_calls=[_call("c1", "read", path="a.py")]),
            _tool_result("c1", "line\n" * 300),
            _Message("assistant", "Done."),
            _Message("user", "second task", "user_input"),
            _Message("assistant", "Sure."),
            _Message("user", "<plan-state>\n1. x\n</plan-state>", "transient"),
        ),
        tools=({"name": "read", "description": "Read", "parameters": {}},),
        tool_schema_tokens=10,
        output_reserve_tokens=0,
    )


def test_system_prompt_is_split_into_sections():
    report = classify_context(_turn_view())
    system = next(item for item in report["categories"] if item["id"] == "system_prompt")
    assert [item["key"] for item in sorted(system["items"], key=lambda item: item["id"])] == [
        "system_role", "system_rules", "system_tools",
    ]
    assert system["count"] == 1


def test_context_adds_up_each_turn():
    report = classify_context(_turn_view())
    first, second = report["turns"]
    assert (first["label"], second["label"]) == ("first task", "second task")
    assert first["parts"]["reasoning"] > 0 and first["parts"]["tool_results"] > first["parts"]["tool_calls"] > 0
    assert second["parts"]["tool_calls"] == 0
    assert first["tokens"] == sum(first["parts"].values())
    # Every conversation token except this request's own reminders belongs to a turn.
    by_id = {item["id"]: item for item in report["categories"]}
    turned = sum(by_id[name]["tokens"] for name in ("user", "assistant", "reasoning", "tool_calls", "tool_output"))
    assert first["tokens"] + second["tokens"] == turned
    assert "turn" not in by_id["plan"]["items"][0]
    # The snapshot carries no content; it is fetched on demand.
    assert all("content" not in item for category in report["categories"] for item in category["items"])


def test_context_detail_lists_every_item_with_its_content():
    report = classify_context(_turn_view(), detail=True)
    turn = context_detail(report, turn=0)
    assert turn["turn"]["label"] == "first task"
    assert [item["category"] for item in turn["items"]] == ["user", "tool_calls", "reasoning", "assistant", "tool_output", "assistant"]
    assert turn["tokens"] == turn["turn"]["tokens"]
    result = next(item for item in turn["items"] if item["category"] == "tool_output")
    assert result["content"] == "line\n" * 300
    call = next(item for item in turn["items"] if item["category"] == "tool_calls")
    assert json.loads(call["content"]) == {"path": "a.py"}
    schemas = context_detail(report, category="tool_schemas")
    assert json.loads(schemas["items"][0]["content"])["name"] == "read"
    role = next(item for item in context_detail(report, category="system_prompt")["items"] if item["key"] == "system_role")
    assert role["content"] == "You are helpful."
    # Ids are stable between the snapshot and the detail view.
    snapshot_ids = {item["id"] for category in classify_context(_turn_view())["categories"] for item in category["items"]}
    assert snapshot_ids <= {item["id"] for category in report["categories"] for item in category["items"]}
    with pytest.raises(KeyError):
        context_detail(report, category="nope")
    with pytest.raises(KeyError):
        context_detail(report, turn=9)
    # The whole request in wire order: system message, tool list, then the conversation.
    order = list(dict.fromkeys(item["category"] for item in context_detail(report)["items"]))
    assert order[:3] == ["system_prompt", "tool_schemas", "user"]


def test_context_detail_caps_long_content():
    view = SimpleNamespace(entries=(_Message("user", "x" * 100_000, "user_input"),), tool_schema_tokens=0, output_reserve_tokens=0)
    item = context_detail(classify_context(view, detail=True), category="user")["items"][0]
    assert item["truncated"] is True
    assert len(item["content"]) < 100_000


def test_preview_folds_old_tool_output_like_the_real_request():
    records = [MessageRecord("m0", {"role": "system", "content": "prompt"}, "history")]
    for index in range(6):
        call_id = f"c{index}"
        records.append(MessageRecord(f"a{index}", {"role": "assistant", "content": "", "tool_calls": [_call(call_id, "shell", command=f"cat {index}")]}, "history"))
        records.append(MessageRecord(f"t{index}", {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"ok": True, "data": "w " * 3000})}, "history"))
    builder = ContextBuilder(ContextCompactor(context_watermark=0.5, keep_recent_tool_results=2))
    view = builder.build(records, tools=[], context_limit=8000)
    report = classify_context(view, preview=True, watermark=0.5, limit=8000)
    assert report["preview"] is True
    # A real view carries no limit of its own; the caller passes it.
    assert report["limit"] == 8000
    assert report["fold_at"] == 4000 - 1000
    assert report["folded"]["count"] == 4
    assert report["folded"]["tokens"] > 0
    outputs = next(item for item in report["categories"] if item["id"] == "tool_output")
    assert [item.get("folded", False) for item in outputs["items"][:2]] == [False, False]
    assert report["used"] < 8000


def test_paths_reject_escape_and_symlink(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8", newline="")
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
    (tmp_path / "widget.py").write_text("def AlphaWidget():\n    return 1\n", encoding="utf-8", newline="")
    (tmp_path / "notes.md").write_text("AlphaWidget is mentioned here\n", encoding="utf-8", newline="")
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
    (first / "keep.txt").write_text("stay", encoding="utf-8", newline="")
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
    tracked.write_text("base\n", encoding="utf-8", newline="")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    tracked.write_text("dirty before task\n", encoding="utf-8", newline="")
    session = SimpleNamespace(session_id="s1", project_root=root, workspace_dir=root)
    journal = SessionChangeJournal(session)
    journal.ensure_baseline()
    preexisting = {item["path"]: item for item in journal.projection()["unowned_changes"]}
    assert preexisting["tracked.txt"]["state"] == "preexisting"
    blocked = journal.revert(["tracked.txt"], confirm=True)
    assert blocked["applied"] is False
    assert tracked.read_text(encoding="utf-8") == "dirty before task\n"

    created = root / "added.txt"
    created.write_text("task\n", encoding="utf-8", newline="")
    journal.note_text(created, before=None, after="task\n", existed=False, call_id="c1", tool_name="write")
    other = root / "other.txt"
    other.write_text("other\n", encoding="utf-8", newline="")
    journal.note_text(other, before=None, after="other\n", existed=False, call_id="c2", tool_name="write")
    created.write_text("external\n", encoding="utf-8", newline="")
    conflicted = journal.revert(["added.txt", "other.txt"], confirm=True)
    assert conflicted["applied"] is False
    assert other.read_text(encoding="utf-8") == "other\n"
    assert created.read_text(encoding="utf-8") == "external\n"

    created.write_text("task\n", encoding="utf-8", newline="")
    reviewed = journal.accept(["added.txt", "other.txt"], confirm=True)
    assert reviewed["applied"] is True
    assert created.read_text(encoding="utf-8") == "task\n"
    states = {item["path"]: item["state"] for item in journal.projection()["changes"]}
    assert states["added.txt"] == "accepted"
    assert states["other.txt"] == "accepted"

    fresh = root / "fresh.txt"
    fresh.write_text("fresh\n", encoding="utf-8", newline="")
    journal.note_text(fresh, before=None, after="fresh\n", existed=False, call_id="c4", tool_name="write")
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
        tool_name="write",
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
    assert not tool_visible("write", SimpleNamespace(authorization=SessionAuthorization("visibility", interaction_mode="ask", permission_mode="bypass")))
    assert tool_visible("update_plan", SimpleNamespace(authorization=SessionAuthorization("visibility", interaction_mode="plan", permission_mode="bypass")))
    from wright.domain.policy.permission.limits import ASK_OPERATIONS as domain_ask
    assert domain_ask == ASK_OPERATIONS
    from wright.domain.policy.permission.limits import PLAN_OPERATIONS as domain_plan
    assert domain_plan == PLAN_OPERATIONS

    session = Session.create("grants", tmp_path)
    permissions = PermissionService(FilePermissionRepository(tmp_path))
    backend = LocalExecutionBackend(tmp_path, session.get_cwd)
    snapshot = permissions.snapshot(session)
    change = resource_change(session, backend, snapshot, {"kind": "file", "path": "a.txt", "operations": ["file_read"]}, "project")
    permissions.commit(change, session, expected_version=snapshot.version)
    listed = list_grants(session, permissions)
    assert session.authorization.rules == ()
    grant = listed["grants"][0]
    permissions.revoke(session, grant["id"], "project", listed["version"])
    assert list_grants(session, permissions)["grants"] == []


def test_directory_grant_can_be_added_and_removed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    root, extra = tmp_path / "root", tmp_path / "extra"
    root.mkdir()
    extra.mkdir()
    session = Session.create("directories", root)
    permissions = PermissionService(FilePermissionRepository(root))
    backend = LocalExecutionBackend(root, session.get_cwd)
    snapshot = permissions.snapshot(session)
    change = resource_change(session, backend, snapshot, {"kind": "directory", "path": str(extra), "operations": ["file_read"]}, "session")
    permissions.commit(change, session, expected_version=snapshot.version)
    listed = list_grants(session, permissions)
    assert listed["grants"][0]["target"] == str(extra.resolve())
    assert listed["grants"][0]["operations"] == ["file_read"]
    permissions.revoke(session, listed["grants"][0]["id"], "session", listed["version"])
    assert permissions.snapshot(session).scope.additional == ()


def test_memory_and_rules_are_project_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    service = MemoryService(SqliteMemoryStore(tmp_path / "memory"))
    created = create_fact(
        service,
        bound_project_id=project_id(one),
        content="only one",
        kind="constraint",
        subject="project",
    )
    visible = project_memory(service, bound_project_id=project_id(one))
    hidden = project_memory(service, bound_project_id=project_id(two))
    assert any(item.get("id") == created["id"] for item in visible["facts"])
    assert all(item.get("id") != created["id"] for item in hidden["facts"])
    with pytest.raises(MemoryViewError, match="another project"):
        forget(service, created["id"], bound_project_id=project_id(two), confirm=True)
    with pytest.raises(Exception, match="confirmation"):
        forget(service, created["id"], bound_project_id=project_id(one), confirm=False)
    assert forget(service, created["id"], bound_project_id=project_id(one), confirm=True)["deleted"]

    saved = save_rule(one, skill_store(), "review-notes", scope="project", description="Review notes", body="Be precise.")
    assert saved["id"] == "review-notes"
    assert any(item.get("id") == "review-notes" for item in list_rules(one, skill_store())["rules"])
    assert all(item.get("id") != "review-notes" for item in list_rules(two, skill_store())["rules"])
    with pytest.raises(Exception, match="confirmation"):
        delete_rule(one, skill_store(), "review-notes", scope="project", confirm=False)
    assert delete_rule(one, skill_store(), "review-notes", scope="project", confirm=True)["deleted"] is True


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
        services=SimpleNamespace(job_scheduler=None),
    )
    created = create_schedule(
        runtime,
        name="nightly",
        prompt="summarize",
        trigger={"type": "interval", "every_seconds": 3600},
    )
    store.close()
    listed = list_project_schedules(open_autonomy_store, project)
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
    history.materialize_due(now=10)
    retained = delete_schedule(runtime, again["id"], confirm=True)
    assert retained["deleted"] is False
    assert retained["retained_for_history"] is True
    assert history.get_job(again["id"]).status == "cancelled"
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
    paused_closed = project_action(open_autonomy_store, project, later["id"], "pause")
    assert paused_closed["status"] == "paused"
    rows = {item["id"]: item for item in list_project_schedules(open_autonomy_store, project)}
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


def test_timeline_projects_reasoning_apart_from_assistant_text():
    class Record:
        def __init__(self, identifier, role, text, source, extra=None):
            self.id = identifier
            self.source = source
            self.message = {"role": role, "content": text, **(extra or {})}

    session = SimpleNamespace(
        message_records=[
            Record("m1", "user", "what tools?", "user_input"),
            Record(
                "m2",
                "assistant",
                "I can read files.",
                "model",
                {"reasoning": "List the tools. Keep the reply short."},
            ),
        ],
        tool_executions={},
    )
    timeline = project_timeline(session)
    assert [item["kind"] for item in timeline] == ["text", "reasoning", "text"]
    assert timeline[1]["id"] == "m2:reason"
    assert timeline[1]["text"] == "List the tools. Keep the reply short."
    assert timeline[2]["id"] == "m2"
    assert timeline[2]["text"] == "I can read files."


def test_checkpoint_relabel_updates_the_display_name(tmp_path: Path):
    from wright.infrastructure.persistence.session.repository import (
        FileSessionRepository,
    )

    store = FileSessionRepository(tmp_path)
    path = store.save(Session.create("old", tmp_path, session_id="sess01ab"))
    result = store.relabel("sess01ab", "Harden Redis rate limiter")
    assert result["user_goal"] == "Harden Redis rate limiter"
    assert '"session_label": "Harden Redis rate limiter"' in path.read_text(encoding="utf-8")


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
    (static / "index.html").write_text("<main>Wright</main>", encoding="utf-8", newline="")
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
    (other / "secret.txt").write_text("hidden", encoding="utf-8", newline="")
    (launch / "visible.txt").write_text("shown", encoding="utf-8", newline="")
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
        fresh = client.get("/api/v1/preferences").json()
        assert fresh["interface_language_saved"] is False
        assert fresh["theme_saved"] is False
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
        assert saved.json()["interface_language_saved"] is True
        assert saved.json()["theme_saved"] is True
        sized = client.put(
            "/api/v1/preferences",
            json={"inspector_width": 420},
            headers={"origin": "http://testserver"},
        )
        assert sized.status_code == 200
        assert sized.json()["inspector_width"] == 420
        wide = client.put(
            "/api/v1/preferences",
            json={"inspector_width": 800},
            headers={"origin": "http://testserver"},
        )
        assert wide.status_code == 200
        assert wide.json()["inspector_width"] == 800
        rejected = client.put(
            "/api/v1/preferences",
            json={"inspector_width": 120},
            headers={"origin": "http://testserver"},
        )
        assert rejected.status_code == 400
        too_wide = client.put(
            "/api/v1/preferences",
            json={"inspector_width": 801},
            headers={"origin": "http://testserver"},
        )
        assert too_wide.status_code == 400
        assert client.get("/api/v1/preferences").json()["sidebar_open"] is True
        rail = client.put(
            "/api/v1/preferences",
            json={"sidebar_open": False, "sidebar_width": 300},
            headers={"origin": "http://testserver"},
        )
        assert rail.status_code == 200
        assert rail.json()["sidebar_open"] is False
        assert rail.json()["sidebar_width"] == 300
        narrow = client.put(
            "/api/v1/preferences",
            json={"sidebar_width": 100},
            headers={"origin": "http://testserver"},
        )
        assert narrow.status_code == 400
        assert client.get("/api/v1/preferences").json()["thought_style"] == "quiet"
        lead = client.put(
            "/api/v1/preferences",
            json={"thought_style": "lead"},
            headers={"origin": "http://testserver"},
        )
        assert lead.status_code == 200
        assert lead.json()["thought_style"] == "lead"
        assert client.get("/api/v1/preferences").json()["thought_style"] == "lead"
        unknown = client.put(
            "/api/v1/preferences",
            json={"thought_style": "loud"},
            headers={"origin": "http://testserver"},
        )
        assert unknown.status_code == 422
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
    # Not a catalog model: it needs an endpoint and key, as a real one would.
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(assembly, "load_env", lambda: None)
    monkeypatch.setattr(assembly, "LLMClient", lambda **_: Model())
    monkeypatch.setattr(assembly, "load_mcp_configs", lambda _: [])
    monkeypatch.setattr(assembly, "optional_knowledge_tools", lambda *args, **kwargs: [])
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
        # No request yet: the Context tab still gets an estimate, not zeros.
        assert body["context_breakdown"]["preview"] is True
        preview = {item["id"]: item["tokens"] for item in body["context_breakdown"]["categories"]}
        assert preview["tool_schemas"] > 0
        assert body["timeline"] == []
        detail = client.get(f"/api/v1/sessions/{session_id}/context/detail", params={"category": "tool_schemas"})
        assert detail.status_code == 200
        schemas = detail.json()["items"]
        assert len(schemas) == detail.json()["category"]["count"]
        assert all(item["content"].startswith("{") for item in schemas)
        assert client.get(f"/api/v1/sessions/{session_id}/context/detail", params={"category": "nope"}).status_code == 404
        assert client.get(f"/api/v1/sessions/{session_id}/context/detail", params={"turn": 0}).status_code == 404
        assert client.get(f"/api/v1/sessions/{session_id}/context/detail").status_code == 400
        # The whole next request in order; nothing has been sent yet.
        whole = client.get(f"/api/v1/sessions/{session_id}/context/request", params={"raw": True}).json()
        assert "source" not in whole and whole["calibrated_used"] is None
        seqs = [item["seq"] for item in whole["items"]]
        assert seqs == sorted(seqs) and whole["items"][0]["category"] == "system_prompt"
        assert whole["request"]["messages"][0]["role"] == "system" and whole["request"]["tools"]
        assert client.get(f"/api/v1/sessions/{session_id}/skills").json() == {"skills": []}
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
    (root / "kept.txt").write_text("leave me\n", encoding="utf-8", newline="")
    (root / "old.txt").write_text("same\n", encoding="utf-8", newline="")
    subprocess.run(["git", "add", "kept.txt", "old.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    (root / "kept.txt").write_text("edited outside the task\n", encoding="utf-8", newline="")
    session = SimpleNamespace(session_id="s-rename", project_root=root, workspace_dir=root)
    journal = SessionChangeJournal(session)
    before = journal.capture()
    (root / "old.txt").rename(root / "new.txt")
    journal.commit_capture(before, call_id="mv1", tool_name="shell")
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


@pytest.mark.parametrize(("status", "listed"), [
    ("awaiting_approval", False), ("failed", False), ("succeeded", True),
    ("running", False),
])
def test_accessed_files_only_lists_tools_that_succeeded(status, listed):
    call = SimpleNamespace(id="c1", name="edit", arguments={"file": "src/a.py"})
    session = SimpleNamespace(tool_executions={"c1": SimpleNamespace(call=call, status=status)})
    assert bool(project_accessed_files(session)) is listed
    read = SimpleNamespace(id="c2", name="read", arguments={"file": "src/a.py"})
    read_session = SimpleNamespace(
        tool_executions={"c2": SimpleNamespace(call=read, status=status)}
    )
    assert bool(project_accessed_files(read_session)) is listed


def test_accessed_files_lists_directories_without_treating_them_as_files():
    listing = SimpleNamespace(id="c3", name="ls", arguments={"directory": "web/src"})
    session = SimpleNamespace(
        tool_executions={"c3": SimpleNamespace(call=listing, status="succeeded")}
    )
    assert project_accessed_files(session) == [
        {"path": "web/src", "access": "list", "tool": "ls", "call_id": "c3", "count": 1,
         "tools": {"ls": 1}, "last_calls": {"ls": "c3"}}
    ]


def test_episode_delete_is_scoped_to_the_bound_project():
    deleted: list[str] = []

    class Episodes:
        def get_episode(self, episode_id):
            return SimpleNamespace(id=episode_id, project_id="other")

        def forget(self, episode_id, **_kwargs):
            deleted.append(episode_id)
            return [episode_id]

    with pytest.raises(MemoryViewError, match="another project"):
        forget(Episodes(), "ep-1", bound_project_id="mine", confirm=True)
    assert deleted == []
    assert forget(Episodes(), "ep-1", bound_project_id="other", confirm=True)["deleted"]
    assert deleted == ["ep-1"]


def test_accessed_files_orders_by_last_access_and_keeps_write_access():
    def run(name, call_id, path):
        call = SimpleNamespace(id=call_id, name=name, arguments={"file": path})
        return call_id, SimpleNamespace(call=call, status="succeeded")

    session = SimpleNamespace(tool_executions=dict([
        run("edit", "c1", "src/a.py"),
        run("read", "c2", "src/b.py"),
        run("read", "c3", "src/a.py"),
    ]))
    assert project_accessed_files(session) == [
        {"path": "src/b.py", "access": "read", "tool": "read", "call_id": "c2", "count": 1,
         "tools": {"read": 1}, "last_calls": {"read": "c2"}},
        {"path": "src/a.py", "access": "write", "tool": "read", "call_id": "c3", "count": 2,
         "tools": {"edit": 1, "read": 1}, "last_calls": {"edit": "c1", "read": "c3"}},
    ]


def test_accessed_files_counts_each_tool_per_path():
    def run(name, call_id, arguments):
        call = SimpleNamespace(id=call_id, name=name, arguments=arguments)
        return call_id, SimpleNamespace(call=call, status="succeeded")

    session = SimpleNamespace(tool_executions=dict([
        run("read", "c1", {"file": "src/a.py"}),
        run("grep", "c2", {"path": "src/a.py"}),
        run("read", "c3", {"file": "src/a.py"}),
        run("write", "c4", {"file": "src/a.py"}),
    ]))
    [entry] = project_accessed_files(session)
    assert entry["access"] == "write"
    assert entry["count"] == 4
    assert entry["tools"] == {"read": 2, "grep": 1, "write": 1}
    assert list(entry["last_calls"].items()) == [("grep", "c2"), ("read", "c3"), ("write", "c4")]


def test_workspace_changes_report_git_status_without_starting_a_session(
    tmp_path: Path, monkeypatch
):
    """History sessions color the file tree from the project's git status alone."""
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "kept.txt").write_text("base\n", encoding="utf-8", newline="")
    subprocess.run(["git", "add", "kept.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    (root / "kept.txt").write_text("edited\n", encoding="utf-8", newline="")
    (root / "new.txt").write_text("new\n", encoding="utf-8", newline="")
    manager = RuntimeManager(root, base_args=argparse.Namespace())
    client = _client(tmp_path, manager)
    try:
        assert client.get("/api/v1/workspaces").status_code == 200
        response = client.get(f"/api/v1/workspaces/{project_id(root)}/changes")
        assert response.status_code == 200
        rows = {row["path"]: row for row in response.json()["changes"]}
        assert set(rows) == {"kept.txt", "new.txt"}
        assert rows["kept.txt"]["status"].strip() == "M"
        assert rows["new.txt"]["untracked"] is True
        assert manager._handles == {}
        missing = client.get("/api/v1/workspaces/unknown/changes")
        assert missing.status_code == 404
    finally:
        client.close()


def test_agent_preview_folds_without_announcing_compaction(tmp_path: Path):
    session = Session.create("preview", tmp_path)
    for index in range(6):
        call_id = f"c{index}"
        session.message_records.append(MessageRecord(f"a{index}", {"role": "assistant", "content": "", "tool_calls": [_call(call_id, "shell", command="cat")]}, "history"))
        session.message_records.append(MessageRecord(f"t{index}", {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"ok": True, "data": "w " * 3000})}, "history"))
    llm = SimpleNamespace(model="m", context_limit=12000)
    agent = create_agent(llm, [], session)
    announced = []
    agent._model_turn.compactor.on_compact = lambda *args: announced.append(args)
    report = agent.preview_context()
    assert report["preview"] is True
    assert report["folded"]["count"] > 0
    assert report["used"] < 12000
    assert announced == []


def test_list_directory_marks_git_ignored_entries(tmp_path: Path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("log/\n.env\n", encoding="utf-8")
    (tmp_path / "log").mkdir()
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    entries = {item["name"]: item for item in list_directory(tmp_path, "")["entries"]}
    assert entries["log"].get("ignored") is True
    assert entries[".env"].get("ignored") is True
    assert "ignored" not in entries["main.py"]
    assert "ignored" not in entries[".gitignore"]


def test_list_directory_outside_git_marks_nothing_ignored(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    entries = list_directory(tmp_path, "")["entries"]
    assert all("ignored" not in item for item in entries)
