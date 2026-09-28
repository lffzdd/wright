"""Scope, identity, deactivation, and provenance for semantic memory.

Scripted models and the tool executor exercise the real service path.
They do not score model quality.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.responses import event
from wright.application.memory.assembly import assemble_memory_manager, memory_tools
from wright.application.memory.extract import extract_from_snapshot
from wright.application.memory.llm_util import SideQueryResult
from wright.application.memory.memory_service import MemoryService
from wright.core.paths import project_id
from wright.domain.gateway.memory import SelectorChoice
from wright.domain.model.memory import (
    SemanticMemoryConflictError,
    SourceLocator,
    encode_locator,
)
from wright.domain.model.session import Session, UsageRecord
from wright.domain.model.tool import ToolCall
from wright.infrastructure.persistence.memory import EpisodeStore, SemanticMemoryStore
from wright.infrastructure.persistence.memory.evidence import SessionEvidenceSource
from wright.infrastructure.persistence.memory.semantic import (
    create_memory,
    get_memory,
    update_memory,
)
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.tools.executor import ConcurrentToolExecutor
from wright.infrastructure.tools.memory import build_memory_tools
from wright.infrastructure.tools.runtime import ToolRuntime


class _QuietLLM:
    model = "quiet"

    def __call__(self, messages, **kwargs):
        raise AssertionError("selector should not run")
        yield


def _service(tmp_path: Path, selector=None, evidence=None) -> MemoryService:
    directory = tmp_path / "memory"
    return MemoryService(
        semantic_store=SemanticMemoryStore(directory),
        episode_store=EpisodeStore(directory),
        selector=selector,
        evidence_source=evidence,
    )


def _project(tmp_path: Path, name: str) -> tuple[Path, str]:
    root = tmp_path / name
    root.mkdir()
    return root, project_id(root)


class _EchoSelector:
    def __init__(self, extra: tuple[str, ...] = ()) -> None:
        self.extra = extra
        self.calls = 0
        self.manifests: list[str] = []

    def select(self, *, task, semantic_manifest, episode_manifest):
        del task, episode_manifest
        self.calls += 1
        self.manifests.append(semantic_manifest)
        ids = []
        for line in semantic_manifest.splitlines():
            if line.startswith("- "):
                ids.append(line[2:].split("|", 1)[0].strip())
        return SelectorChoice(memory_ids=(*ids, *self.extra), episode_ids=())


class _FailedSelector:
    def __init__(self) -> None:
        self.calls = 0

    def select(self, *, task, semantic_manifest, episode_manifest):
        del task, semantic_manifest, episode_manifest
        self.calls += 1
        return SelectorChoice(failed=True, episodes_usable=False, failure_type="RuntimeError")


def test_projects_do_not_share_memories_or_indexes(tmp_path: Path):
    _root_a, project_a = _project(tmp_path, "alpha")
    _root_b, project_b = _project(tmp_path, "beta")
    service = _service(tmp_path, selector=_EchoSelector(extra=("mem-foreign",)))
    for index in range(210):
        service.create_semantic(
            name="package-manager",
            description=f"noise {index}",
            type_="project",
            content=f"OTHER_PROJECT_TOKEN {index}",
            project_id=project_b,
        )
    kept, error = service.create_semantic(
        name="package-manager",
        description="alpha constraint",
        type_="user",
        content="项目 A 必须使用 bun",
        project_id=project_a,
    )
    assert error is None and kept is not None
    global_memory, error = service.create_semantic(
        name="editor",
        description="全局偏好使用 dark theme",
        type_="user",
        content="全局偏好使用 dark theme",
        scope="global",
    )
    assert error is None and global_memory is not None
    assert kept.id != global_memory.id

    context_a = service.prepare_memory_context("bun", project_id=project_a)
    text_a = context_a.semantic_text + context_a.prompt_injection
    assert "项目 A 必须使用 bun" in text_a
    assert "全局偏好使用 dark theme" in text_a
    assert "OTHER_PROJECT_TOKEN" not in text_a
    assert "noise 0" not in text_a
    assert "mem-foreign" not in text_a
    assert kept.id in context_a.selected_memory_ids

    context_b = service.prepare_memory_context("bun", project_id=project_b)
    text_b = context_b.semantic_text + context_b.prompt_injection
    assert "项目 A 必须使用 bun" not in text_b
    assert "全局偏好使用 dark theme" in text_b
    assert f"project={project_b}" in text_b
    assert "noise 209" in text_b

    poisoned = tmp_path / "memory" / "MEMORY.md"
    poisoned.write_text("STALE_INDEX_LEAK OTHER_PROJECT_TOKEN\n", encoding="utf-8")
    fresh = _service(tmp_path, selector=_FailedSelector()).prepare_memory_context(
        "bun", project_id=project_a
    )
    failed_text = fresh.semantic_text + fresh.prompt_injection
    assert fresh.selector_failed is True
    assert "项目 A 必须使用 bun" not in failed_text  # failure injects the index, not every body
    assert "alpha constraint" in failed_text
    assert "OTHER_PROJECT_TOKEN" not in failed_text
    assert "STALE_INDEX_LEAK" not in failed_text

    unscoped = _service(tmp_path).prepare_memory_context("bun", project_id="")
    alone = unscoped.semantic_text
    assert "全局偏好使用 dark theme" in alone
    assert "项目 A 必须使用 bun" not in alone
    assert "OTHER_PROJECT_TOKEN" not in alone


def test_create_requires_project_and_same_name_does_not_overwrite(tmp_path: Path):
    service = _service(tmp_path)
    missing, error = service.create_semantic(
        name="package-manager",
        content="必须使用 bun",
        type_="user",
    )
    assert missing is None
    assert error and "项目" in error
    assert list((tmp_path / "memory").glob("mem-*.md")) == []

    _root, project_a = _project(tmp_path, "alpha")
    first, error = service.create_semantic(
        name="package-manager", content="A 使用 bun", type_="user", project_id=project_a
    )
    second, error = service.create_semantic(
        name="package-manager", content="A 也可以 pnpm", type_="user", project_id=project_a
    )
    assert error is None and first and second
    assert first.id != second.id
    assert get_memory(first.id, tmp_path / "memory").content == "A 使用 bun"
    renamed, error = service.update_semantic(
        first.id,
        expected_revision=first.revision,
        project_id=project_a,
        name="包管理器",
    )
    assert error is None and renamed is not None
    assert renamed.id == first.id
    assert renamed.name == "包管理器"
    unknown, error = service.update_semantic(
        "mem-does-not-exist",
        expected_revision=1,
        project_id=project_a,
        content="nope",
    )
    assert unknown is None and error


def test_inactive_stays_out_of_recall_until_reactivated(tmp_path: Path):
    _root, project_a = _project(tmp_path, "alpha")
    service = _service(tmp_path, selector=_EchoSelector())
    created, error = service.create_semantic(
        name="rule", content="必须使用 bun", type_="feedback", project_id=project_a
    )
    assert error is None and created is not None
    inactive, error = service.update_semantic(
        created.id,
        expected_revision=created.revision,
        project_id=project_a,
        status="inactive",
    )
    assert error is None and inactive is not None and inactive.status == "inactive"
    assert (tmp_path / "memory" / f"{created.id}.md").is_file()
    hidden = service.prepare_memory_context("bun", project_id=project_a)
    assert created.id not in hidden.semantic_text
    assert "必须使用 bun" not in hidden.semantic_text
    found, _reads, error = service.get_semantic(created.id, project_id=project_a)
    assert error is None and found is not None and found.status == "inactive"
    searched, error = service.search_semantic("bun", project_id=project_a)
    assert error is None and searched == []
    edited, error = service.update_semantic(
        created.id,
        expected_revision=inactive.revision,
        project_id=project_a,
        content="必须使用 bun，正文改了",
    )
    assert error is None and edited is not None
    assert edited.status == "inactive"
    assert edited.content == "必须使用 bun，正文改了"
    restored, error = service.update_semantic(
        created.id,
        expected_revision=edited.revision,
        project_id=project_a,
        status="active",
    )
    assert error is None and restored is not None and restored.status == "active"
    visible = service.prepare_memory_context("bun", project_id=project_a)
    assert "必须使用 bun，正文改了" in visible.semantic_text
    removed, error = service.delete_semantic(created.id, project_id=project_a)
    assert error is None and removed is not None
    assert not (tmp_path / "memory" / f"{created.id}.md").exists()


def test_same_turn_deactivation_drops_cached_recall(tmp_path: Path):
    root, project_a = _project(tmp_path, "alpha")
    holder = {"id": ""}

    class _SelectorLLM:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, messages, **kwargs):
            del messages, kwargs
            self.calls += 1
            payload = {"selected_memories": [holder["id"]], "selected_episodes": []}
            yield event(content=json.dumps(payload), reasoning="")

    selector = _SelectorLLM()
    manager = assemble_memory_manager(_QuietLLM(), selector_llm=selector, directory=tmp_path / "memory")
    manager.bind_project(root)
    created, error = manager.service.create_semantic(
        name="rule", content="回合内必须使用 bun", type_="project", project_id=project_a
    )
    assert error is None and created is not None
    holder["id"] = created.id
    global_memory, error = manager.service.create_semantic(
        name="editor",
        description="全局偏好",
        content="全局偏好使用 dark theme",
        type_="user",
        scope="global",
    )
    assert error is None and global_memory is not None
    session = SimpleNamespace(
        project_root=root,
        workspace_dir=root,
        agent_root_turn_id="turn-1",
        current_goal=lambda: "bun",
    )
    first = manager.recall_for_turn(session)
    assert "回合内必须使用 bun" in first.semantic_text
    assert selector.calls == 1

    tools = {tool.name: tool for tool in memory_tools(manager)}
    result = _execute(tools["update_memory"], {
        "memory_id": created.id,
        "expected_revision": created.revision,
        "status": "inactive",
    })
    assert result["status"] == "inactive"
    second = manager.recall_for_turn(session)
    assert "回合内必须使用 bun" not in second.semantic_text
    assert selector.calls == 1

    other = tmp_path / "beta"
    other.mkdir()
    manager.bind_project(other)
    session.project_root = other
    session.workspace_dir = other
    manager.recall_for_turn(session)
    assert selector.calls == 2
    assert "回合内必须使用 bun" not in manager.recall_for_turn(session).semantic_text


def test_stale_revision_conflicts_and_failed_write_is_atomic(tmp_path: Path, monkeypatch):
    created = create_memory("n", "d", "user", "v1", tmp_path, scope="global")
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from wright.infrastructure.persistence.memory.semantic import update_memory\n"
        "update_memory(sys.argv[2], content='from-child', "
        "expected_revision=int(sys.argv[3]), directory=Path(sys.argv[1]))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), created.id, str(created.revision)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    with pytest.raises(SemanticMemoryConflictError):
        update_memory(
            created.id,
            content="stale",
            expected_revision=created.revision,
            directory=tmp_path,
        )
    assert get_memory(created.id, tmp_path).content == "from-child"

    create_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from wright.infrastructure.persistence.memory.semantic import create_memory\n"
        "create_memory(sys.argv[2], 'd', 'user', 'body-'+sys.argv[2], "
        "Path(sys.argv[1]), scope='global')\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", create_script, str(tmp_path), name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("left", "right")
    ]
    for process in processes:
        stdout, stderr = process.communicate()
        assert process.returncode == 0, stderr
        del stdout
    stored = list((tmp_path).glob("mem-*.md"))
    assert len(stored) >= 3
    assert all(path.read_text(encoding="utf-8").count("---") >= 2 for path in stored)

    import wright.infrastructure.persistence.memory.semantic as semantic

    def explode(source, destination):
        del source, destination
        raise OSError("replace failed")

    monkeypatch.setattr(semantic.os, "replace", explode)
    with pytest.raises(OSError):
        create_memory("broken", "d", "user", "nope", tmp_path, scope="global")
    assert list(tmp_path.glob("*.tmp")) == []
    assert get_memory(created.id, tmp_path).content == "from-child"


def test_old_semantic_files_are_ignored(tmp_path: Path):
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "old-note.md").write_text(
        "---\nname: old-note\ndescription: legacy\ntype: feedback\n"
        "created_at: 2024-01-01T00:00:00+00:00\nupdated_at: 2024-01-01T00:00:00+00:00\n"
        "origin: user_statement\nsource_refs: ev-u-msg_1\n"
        "---\n\nlegacy body stays\n",
        encoding="utf-8",
    )
    _root, project_a = _project(tmp_path, "alpha")
    service = _service(tmp_path, selector=_EchoSelector())
    context = service.prepare_memory_context("legacy", project_id=project_a)
    assert "legacy body stays" not in context.semantic_text
    assert context.memories == ()
    _record, _reads, error = service.get_semantic("old-note", project_id=project_a)
    assert _record is None and error and "不是当前语义记忆格式" in error
    listed, list_error = service.search_semantic("", project_id=project_a)
    assert list_error is None and listed == []
    assert (directory / "old-note.md").read_text(encoding="utf-8").endswith("legacy body stays\n")


def test_extract_scope_budget_and_locators(tmp_path: Path):
    directory = tmp_path / "memory"
    _root_a, project_a = _project(tmp_path, "alpha")
    _root_b, project_b = _project(tmp_path, "beta")
    service = _service(tmp_path)
    global_memory, error = service.create_semantic(
        name="package-manager",
        description="global bun",
        content="全局必须使用 bun",
        type_="user",
        scope="global",
    )
    assert error is None and global_memory is not None
    snapshot = _snapshot(project_a)
    dropped = "ev-u-dropped"
    snapshot["episode"]["evidence"].append({
        "id": dropped,
        "kind": "user_statement",
        "summary": "记住 " + ("额外来源" * 2000),
        "session_id": "sess-a",
        "root_run_id": "root-a",
        "message_id": "msg_9",
    })

    def run(payload: str, *, project: str = project_a):
        body = _snapshot(project)
        if project == project_a:
            body["episode"]["evidence"].append(snapshot["episode"]["evidence"][-1])
        return extract_from_snapshot(
            body,
            query=lambda _system, _user: SideQueryResult(
                payload, False, "", UsageRecord(1, 1, 2), 1.0, "scripted"
            ),
            directory=directory,
            service=service,
        )

    cited_drop = run(json.dumps({"memories": [{
        "name": "package-manager",
        "type": "user",
        "content": "以后只用 bun",
        "action": "create",
        "source_refs": [dropped],
    }]}))
    assert cited_drop.written == 0
    assert "unknown_source_ref" in cited_drop.reason_codes

    saved = run(json.dumps({"memories": [{
        "name": "package-manager",
        "type": "user",
        "content": "项目里以后只用 bun",
        "action": "create",
        "source_refs": ["ev-u-1"],
    }]}))
    assert saved.written == 1
    project_rows = service.semantic_store.list(read_scope="current_project", project_id=project_a)
    assert len(project_rows) == 1
    assert project_rows[0].project_id == project_a
    assert project_rows[0].locators[0].session_id == "sess-a"
    assert get_memory(global_memory.id, directory).content == "全局必须使用 bun"

    stolen = run(json.dumps({"memories": [{
        "memory_id": global_memory.id,
        "name": "package-manager",
        "type": "user",
        "content": "把全局改成项目意见",
        "action": "update",
        "source_refs": ["ev-u-1"],
    }]}))
    assert stolen.written == 0
    assert get_memory(global_memory.id, directory).content == "全局必须使用 bun"

    missing_id = run(json.dumps({"memories": [{
        "memory_id": "mem-not-offered",
        "name": "package-manager",
        "type": "user",
        "content": "偷偷更新",
        "action": "update",
        "source_refs": ["ev-u-1"],
    }]}))
    assert missing_id.written == 0
    other = run(json.dumps({"memories": [{
        "name": "package-manager",
        "type": "user",
        "content": "项目 B 使用 pnpm",
        "action": "create",
        "source_refs": ["ev-u-1"],
    }]}), project=project_b)
    assert other.written == 1
    rows_b = service.semantic_store.list(read_scope="current_project", project_id=project_b)
    assert rows_b[0].content == "项目 B 使用 pnpm"
    assert rows_b[0].id != project_rows[0].id


def test_evidence_survives_reload_and_missing_session(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = Session.create("第一会话只用 bun", workspace)
    first.begin_user_turn("第一会话只用 bun")
    first.append_message({"role": "user", "content": "第一会话只用 bun"})
    second = Session.create("第二会话不要被绑上", workspace)
    second.begin_user_turn("第二会话不要被绑上")
    second.append_message({"role": "user", "content": "第二会话不要被绑上"})
    repo = FileSessionRepository(tmp_path / "checkpoints")
    repo.save(first)
    repo.save(second)
    reloaded = FileSessionRepository(tmp_path / "checkpoints").load(first.session_id)
    assert reloaded is not None
    user = next(record for record in reloaded.message_records if record.source == "user_input")
    run = reloaded.current_run() or reloaded.active_run()
    assert run is not None
    root_run_id = str(run.root_run_id or run.run_id)
    token = encode_locator(SourceLocator(
        session_id=reloaded.session_id,
        root_run_id=root_run_id,
        kind="user_statement",
        message_id=user.id,
        local_id="ev-u-msg_1",
        episode_id="ep-a",
    ))
    directory = tmp_path / "memory"
    created = create_memory(
        "bun",
        "from the first session",
        "user",
        "第一会话只用 bun",
        directory,
        scope="global",
        origin="user_statement",
        source_refs=(token,),
    )
    service = MemoryService(
        SemanticMemoryStore(directory),
        EpisodeStore(directory),
        evidence_source=SessionEvidenceSource(repo),
    )
    plain, reads, error = service.get_semantic(created.id, include_evidence=False)
    assert error is None and plain is not None and reads == ()
    loaded, reads, error = service.get_semantic(created.id, include_evidence=True)
    assert error is None and loaded is not None
    assert reads[0].status == "available"
    assert reads[0].text == "第一会话只用 bun"
    assert "第二会话" not in reads[0].text

    missing = encode_locator(SourceLocator(
        session_id="missing-session",
        root_run_id="root-a",
        kind="user_statement",
        message_id="msg_1",
        local_id="ev-u-msg_1",
    ))
    gone = update_memory(
        created.id,
        expected_revision=created.revision,
        directory=directory,
        origin="user_statement",
        source_refs=(missing,),
    )
    unavailable, reads, error = service.get_semantic(gone.id, include_evidence=True)
    assert error is None and unavailable is not None
    assert reads[0].status == "unavailable"
    assert "第二会话" not in reads[0].text
    assert "第一会话" not in reads[0].text
    with pytest.raises(Exception, match="session"):
        update_memory(
            created.id,
            expected_revision=gone.revision,
            directory=directory,
            origin="user_statement",
            source_refs=("ev-u-msg_1",),
        )


def test_memory_tools_use_the_executor(tmp_path: Path):
    _root, project_a = _project(tmp_path, "alpha")
    _other, project_b = _project(tmp_path, "beta")
    service = _service(tmp_path)
    tools = {
        tool.name: tool
        for tool in build_memory_tools(service=service, project_id=project_a)
    }
    created = _execute(tools["create_memory"], {
        "name": "package-manager",
        "description": "alpha",
        "type": "user",
        "content": "A 必须使用 bun",
    })
    assert created["scope"] == "project"
    assert created["project_id"] == project_a
    assert created["origin"] == "explicit"
    assert created["source_refs"] == []
    other_view = service.prepare_memory_context("bun", project_id=project_b)
    assert "A 必须使用 bun" not in other_view.semantic_text
    fetched = _execute(tools["get_memory"], {"memory_id": created["id"]})
    assert fetched["content"] == "A 必须使用 bun"
    updated = _execute(tools["update_memory"], {
        "memory_id": created["id"],
        "expected_revision": created["revision"],
        "content": "A 改为 pnpm",
    })
    assert updated["content"] == "A 改为 pnpm"
    assert updated["id"] == created["id"]
    assert updated["origin"] == ""
    conflict = tools["update_memory"].call({
        "memory_id": created["id"],
        "expected_revision": created["revision"],
        "content": "stale",
    }, ToolRuntime(tool_name="update_memory"))
    assert not conflict.ok
    found = _execute(tools["search_memory"], {"query": "pnpm"})
    assert found["count"] == 1
    missed = _execute(tools["search_memory"], {"query": "does-not-match"})
    assert missed["count"] == 0


def test_background_snapshot_keeps_its_project(tmp_path: Path):
    from tests.memory.test_evidence_extract_meter import _manager, _session

    session = _session(tmp_path, "记住我以后只用 bun")
    original = project_id(session.workspace_dir)
    task = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=None,
        tool_call_id="spawn_1",
        depth=1,
        task="inspect",
        requested_steps=2,
    )
    old_turn = session.agent_root_turn_id
    session.mark_completed()
    payload = json.dumps({"memories": [{
        "name": "package-manager",
        "type": "user",
        "content": "旧项目只用 bun",
        "action": "create",
        "source_refs": ["placeholder"],
    }]})

    class _CitingLLM:
        model = "scripted"

        def __call__(self, messages, **kwargs):
            del kwargs
            text = str(messages[-1].get("content") if messages else "")
            found = ""
            for token in text.split():
                if token.startswith("[ev-"):
                    found = token[1:].split()[0]
                    break
            body = payload.replace("placeholder", found or "ev-missing")
            yield event(content=body, reasoning="")

    manager = _manager(tmp_path, _CitingLLM())
    deferred = manager.finalize_turn(session, "先记下", extract_semantic=True)
    assert deferred["pending"] is True
    other = tmp_path / "other-project"
    other.mkdir()
    manager.bind_project(other)
    session.control_plane.finish_task(task.id, status="completed", steps_used=1, result="done")
    saved = manager.settle_previous_turn(session, root_turn_id=old_turn, task={"id": task.id})
    assert saved["semantic_memories_written"] == 1
    rows = manager.service.semantic_store.list(read_scope="current_project", project_id=original)
    assert rows and rows[0].content == "旧项目只用 bun"
    assert rows[0].project_id == original
    assert rows[0].project_id != project_id(other)


def test_project_constraint_survives_update_deactivation_and_reload(tmp_path: Path):
    """One chain: two projects, one global preference, then update, deactivate, and reload."""
    _root_a, project_a = _project(tmp_path, "alpha")
    _root_b, project_b = _project(tmp_path, "beta")
    service = _service(tmp_path, selector=_EchoSelector())
    source = encode_locator(SourceLocator(
        session_id="sess-a",
        root_run_id="root-a",
        kind="user_statement",
        message_id="msg_1",
        local_id="ev-u-msg_1",
        episode_id="ep-a",
    ))
    kept, error = service.record_extracted(
        name="package-manager",
        description="alpha constraint",
        type_="user",
        content="项目 A 必须使用 bun",
        origin="user_statement",
        source_refs=(source,),
        project_id=project_a,
    )
    assert error is None and kept is not None
    other, error = service.record_extracted(
        name="package-manager",
        description="beta constraint",
        type_="user",
        content="项目 B 必须使用 pnpm",
        origin="user_statement",
        source_refs=(source.replace("sess-a", "sess-b").replace("root-a", "root-b"),),
        project_id=project_b,
    )
    assert error is None and other is not None
    assert other.id != kept.id
    global_memory, error = service.create_semantic(
        name="editor",
        description="全局偏好使用 dark theme",
        type_="user",
        content="全局偏好使用 dark theme",
        scope="global",
    )
    assert error is None and global_memory is not None

    text_a = service.prepare_memory_context("bun", project_id=project_a).prompt_injection
    text_b = service.prepare_memory_context("pnpm", project_id=project_b).prompt_injection
    assert "项目 A 必须使用 bun" in text_a
    assert "项目 B 必须使用 pnpm" not in text_a
    assert "全局偏好使用 dark theme" in text_a
    assert "项目 B 必须使用 pnpm" in text_b
    assert "项目 A 必须使用 bun" not in text_b
    assert "全局偏好使用 dark theme" in text_b

    updated, error = service.update_semantic(
        kept.id,
        expected_revision=kept.revision,
        project_id=project_a,
        content="项目 A 必须使用 bun 1.1",
    )
    assert error is None and updated is not None
    assert updated.id == kept.id
    assert updated.created_at == kept.created_at
    assert updated.source_refs == ()
    assert updated.status == "active"

    inactive, error = service.update_semantic(
        kept.id,
        expected_revision=updated.revision,
        project_id=project_a,
        status="inactive",
    )
    assert error is None and inactive is not None
    assert inactive.status == "inactive"
    assert inactive.content == "项目 A 必须使用 bun 1.1"
    after = service.prepare_memory_context("bun", project_id=project_a)
    assert "项目 A 必须使用 bun" not in after.prompt_injection
    assert "全局偏好使用 dark theme" in after.prompt_injection

    viewed, reads, error = service.get_semantic(
        kept.id, project_id=project_a, include_evidence=True
    )
    assert error is None and viewed is not None
    assert viewed.status == "inactive"
    assert viewed.content == "项目 A 必须使用 bun 1.1"
    assert viewed.origin == ""
    assert reads == ()
    hidden, _hidden_reads, hidden_error = service.get_semantic(
        kept.id, project_id=project_b
    )
    assert hidden is None
    assert hidden_error is not None

    reloaded = _service(tmp_path)
    again, _reads, error = reloaded.get_semantic(kept.id, project_id=project_a)
    assert error is None and again is not None
    assert again.content == "项目 A 必须使用 bun 1.1"
    assert again.status == "inactive"
    assert again.id == kept.id
    still_b, _reads, error = reloaded.get_semantic(other.id, project_id=project_b)
    assert error is None and still_b is not None
    assert still_b.content == "项目 B 必须使用 pnpm"
    assert still_b.source_refs


def _snapshot(project: str) -> dict:
    return {
        "episode": {
            "id": "ep-a",
            "project_id": project,
            "session_id": "sess-a",
            "root_run_id": "root-a",
            "evidence": [{
                "id": "ev-u-1",
                "kind": "user_statement",
                "summary": "记住我以后只用 bun",
                "session_id": "sess-a",
                "root_run_id": "root-a",
                "message_id": "msg_1",
            }],
            "verification": [],
        }
    }


def _execute(tool, arguments: dict) -> dict:
    prepared = SimpleNamespace(
        tool=tool,
        call=ToolCall(tool.name, arguments, f"call-{tool.name}"),
        local_cancel=threading.Event(),
        effective_timeout=5,
        approval_wait_ms=0,
        final_arguments=arguments,
        runtime=ToolRuntime(tool_name=tool.name),
        resolution=SimpleNamespace(decision="allow", reason="test", source="test"),
        execution=None,
    )
    outcome = ConcurrentToolExecutor(journal=None).execute_batch([(0, prepared)])[0]
    assert outcome.result.ok, outcome.result.err
    return outcome.result.data
