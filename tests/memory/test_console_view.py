"""The memory console: one workspace sees and changes only its own records."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.memory.helpers import make_service, write
from wright.application.memory.service import MemoryServiceError
from wright.application.workspace.memory_view import (
    MemoryViewError,
    create_commitment,
    create_fact,
    forget,
    project_memory,
    revert_change,
    update_commitment,
)
from wright.domain.model.memory import GLOBAL_SPACE_ID, repo_space_id
from wright.infrastructure.persistence.memory import SqliteMemoryStore


def test_change_log_shows_only_visible_spaces(tmp_path):
    service = make_service(tmp_path)
    service.ensure_repo_space("proj-b", "/work/proj-b")
    mine = create_fact(service, bound_project_id="proj-a", content="Use uv in proj-a.", kind="fact")
    theirs = create_fact(service, bound_project_id="proj-b", content="proj-b deploys to db.corp.internal.", kind="fact")
    shared = create_fact(service, bound_project_id="proj-b", content="Prefer short answers.", kind="preference", space="global")

    targets = {item["target_id"] for item in project_memory(service, bound_project_id="proj-a")["changes"]}

    assert mine["id"] in targets
    assert shared["id"] in targets
    assert theirs["id"] not in targets


def test_change_log_is_filtered_before_the_limit(tmp_path):
    service = make_service(tmp_path)
    service.ensure_repo_space("proj-b", "/work/proj-b")
    mine = create_fact(service, bound_project_id="proj-a", content="Only proj-a fact.", kind="fact")
    for index in range(60):
        create_fact(service, bound_project_id="proj-b", content=f"proj-b fact number {index} about topic {index * 7}", kind="fact")

    targets = [item["target_id"] for item in project_memory(service, bound_project_id="proj-a")["changes"]]

    assert targets == [mine["id"]]


def test_console_cannot_revert_another_projects_change(tmp_path):
    service = make_service(tmp_path)
    service.ensure_repo_space("proj-b", "/work/proj-b")
    theirs = create_fact(service, bound_project_id="proj-b", content="proj-b only.", kind="fact")
    change_id = service.changes(target_id=theirs["id"])[0].id

    with pytest.raises(MemoryViewError, match="another project"):
        revert_change(service, change_id, bound_project_id="proj-a")
    assert service.get_fact(theirs["id"]) is not None


def test_forget_scrubs_earlier_snapshots_and_keeps_scope(tmp_path):
    service = make_service(tmp_path)
    fact = write(service, "Home router password hint is the cat's name.").fact
    service.update_fact(fact.id, content="Home router hint lives in the drawer.", actor="user")

    service.forget(fact.id, actor="user")

    history = service.changes(target_id=fact.id)
    assert history
    assert all(change.before is None and change.after is None for change in history)
    assert all(change.scope == repo_space_id("proj-a") for change in history)
    with pytest.raises(MemoryServiceError, match="not kept"):
        service.revert(history[-1].id)


def test_forgotten_records_stay_in_their_projects_log(tmp_path):
    service = make_service(tmp_path)
    service.ensure_repo_space("proj-b", "/work/proj-b")
    fact = create_fact(service, bound_project_id="proj-a", content="Temporary note.", kind="fact")
    forget(service, fact["id"], bound_project_id="proj-a", confirm=True)

    mine = [item for item in project_memory(service, bound_project_id="proj-a")["changes"] if item["action"] == "forget"]
    theirs = [item for item in project_memory(service, bound_project_id="proj-b")["changes"] if item["action"] == "forget"]

    assert [item["target_id"] for item in mine] == [fact["id"]]
    assert theirs == []


def test_legacy_change_rows_get_a_scope(tmp_path):
    directory = tmp_path / "memory"
    service = make_service(tmp_path)
    fact = write(service, "Use uv.").fact
    global_fact = write(service, "Prefer tea.", kind="preference", space=GLOBAL_SPACE_ID).fact
    database = directory / "memory.db"
    service.store.close()
    with sqlite3.connect(database) as conn:
        rows = conn.execute("SELECT id, target_id, before, after FROM changes").fetchall()
        conn.execute("DROP TABLE changes")
        conn.execute(
            "CREATE TABLE changes (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,"
            " action TEXT NOT NULL, target_id TEXT NOT NULL, before TEXT, after TEXT,"
            " note TEXT NOT NULL DEFAULT '', reverted INTEGER NOT NULL DEFAULT 0)"
        )
        for change_id, target_id, before, after in rows:
            conn.execute(
                "INSERT INTO changes(id, at, actor, action, target_id, before, after) VALUES (?, '', 'user', 'create', ?, ?, ?)",
                (change_id, target_id, before, after),
            )
        conn.execute(
            "INSERT INTO changes(at, actor, action, target_id, after) VALUES ('', 'consolidation', 'compact', 'ep-x', ?)",
            (json.dumps({"id": "ep-x", "project_id": "proj-a"}),),
        )

    reopened = SqliteMemoryStore(directory)
    scopes = {change.target_id: change.scope for change in reopened.list_changes()}

    assert scopes[fact.id] == repo_space_id("proj-a")
    assert scopes[global_fact.id] == GLOBAL_SPACE_ID
    assert scopes["ep-x"] == repo_space_id("proj-a")


def test_console_creates_and_reschedules_commitments(tmp_path):
    service = make_service(tmp_path)
    created = create_commitment(service, bound_project_id="proj-a", content="Ping the user about the release.", due="2026-12-01")
    assert created["space_id"] == repo_space_id("proj-a")

    moved = update_commitment(service, created["id"], bound_project_id="proj-a", due="2026-12-05")
    assert moved["due"] == "2026-12-05"
    assert moved["status"] == "open"

    service.ensure_repo_space("proj-b", "/work/proj-b")
    with pytest.raises(MemoryViewError, match="another project"):
        update_commitment(service, created["id"], bound_project_id="proj-b", status="done")
