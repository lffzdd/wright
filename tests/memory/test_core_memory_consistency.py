"""Request freshness and real concurrent core-memory updates."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.memory.test_agent_integration import EmptySelectorLLM
from tests.memory.test_core_memory import DummyEpisodeStore, DummySemanticStore
from tests.responses import response
from wright.application.agent import create_agent
from wright.application.memory.assembly import assemble_memory_manager, memory_tools
from wright.application.memory.memory_service import MemoryService
from wright.core.paths import project_id
from wright.domain.model.memory import CoreMemoryStoreError
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.domain.policy import PermissionResolver, PermissionResponse
from wright.domain.policy.memory import CoreMemoryPolicy
from wright.infrastructure.persistence.memory import (
    CORE_MEMORY_FILE,
    FileCoreMemoryStore,
)
from wright.infrastructure.tools.executor import ConcurrentToolExecutor
from wright.infrastructure.tools.runtime import ToolRuntime


def _service(directory: Path, policy: CoreMemoryPolicy | None = None) -> MemoryService:
    return MemoryService(
        DummySemanticStore(),
        DummyEpisodeStore(),
        core_memory_store=FileCoreMemoryStore(directory),
        core_memory_policy=policy,
    )


def _write_legacy(directory: Path, *, profile: str, anchor: str, persona: str | None = None) -> Path:
    payload = {
        "persona": persona or "Kept persona.",
        "human_profile": profile,
        "project_anchor": anchor,
        "updated_at": "1",
    }
    path = directory / CORE_MEMORY_FILE
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("mode", ["apend", "REPLACE", "", None])
def test_invalid_mode_rejected_without_touching_storage(tmp_path, mode):
    service = _service(tmp_path)
    updated, error = service.update_core_memory("human_profile", "replacement", mode)
    assert updated is None
    assert "Invalid core memory mode" in error
    assert not list(tmp_path.iterdir())

    service.update_core_memory("human_profile", "preserved", "replace")
    memory_file = tmp_path / CORE_MEMORY_FILE
    original = memory_file.read_bytes()
    updated, error = service.update_core_memory("human_profile", "replacement", mode)
    assert updated is None
    assert memory_file.read_bytes() == original


def test_blank_replace_is_not_clear_and_clear_stays_empty(tmp_path):
    service = _service(tmp_path)
    pid = project_id(tmp_path / "alpha")
    service.update_core_memory("human_profile", "kept profile", "replace")
    service.update_core_memory("project_anchor", "kept anchor", "replace", project_id=pid)
    profile_file = tmp_path / CORE_MEMORY_FILE
    anchor_file = FileCoreMemoryStore(tmp_path).project_path(pid)
    original_profile = profile_file.read_bytes()
    original_anchor = anchor_file.read_bytes()

    updated, error = service.update_core_memory("human_profile", "   ", "replace")
    assert updated is None
    assert "cannot be empty" in error
    assert profile_file.read_bytes() == original_profile

    updated, error = service.update_core_memory("project_anchor", "", "replace", project_id=pid)
    assert updated is None
    assert anchor_file.read_bytes() == original_anchor

    updated, error = service.update_core_memory("persona", "", "clear")
    assert updated is None
    assert "prohibited" in error

    updated, error = service.update_core_memory("human_profile", "", "clear")
    assert error is None and updated is not None
    assert updated.content == ""
    assert updated.scope == "global"
    updated, error = service.update_core_memory("project_anchor", "ignored", "clear", project_id=pid)
    assert error is None and updated is not None
    assert updated.content == ""
    assert updated.project_id == pid

    viewed = service.get_core_memory(pid)
    assert viewed is not None
    assert viewed.human_profile == ""
    assert viewed.project_anchor == ""
    rendered = viewed.render_block()
    assert "kept profile" not in rendered
    assert "kept anchor" not in rendered
    assert "Human Profile" not in rendered
    assert "Project Core Anchor" not in rendered
    assert json.loads(profile_file.read_text(encoding="utf-8"))["human_profile"] == ""
    assert json.loads(anchor_file.read_text(encoding="utf-8"))["project_anchor"] == ""


def test_rejected_append_preserves_file_and_releases_lock(tmp_path):
    service = _service(tmp_path)
    service.update_core_memory("human_profile", "x" * 1500, "replace")
    memory_file = tmp_path / CORE_MEMORY_FILE
    original = memory_file.read_bytes()

    updated, error = service.update_core_memory("human_profile", "overflow", "append")
    assert updated is None
    assert "exceeds max limit" in error
    assert str(1500 + len("\n- overflow")) in error
    assert memory_file.read_bytes() == original
    assert (tmp_path / ".core_memory.lock").is_file()

    updated, error = _service(tmp_path).update_core_memory("human_profile", "recovered", "replace")
    assert error is None
    assert updated.content == "recovered"


def test_failed_mutation_corrupt_storage_and_failed_replace_keep_the_old_file(tmp_path, monkeypatch):
    store = FileCoreMemoryStore(tmp_path)

    def seed(record):
        record.human_profile = "preserved"

    store.update_global(seed)
    original = store.file_path.read_bytes()

    def fail_after_mutation(record):
        record.human_profile = "not committed"
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        store.update_global(fail_after_mutation)
    assert store.file_path.read_bytes() == original

    store.file_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _service(tmp_path).update_core_memory("human_profile", "new", "append")
    assert store.file_path.read_text(encoding="utf-8") == "{broken"

    leftover = tmp_path / CORE_MEMORY_FILE
    leftover.write_text(
        json.dumps({
            "persona": "Kept persona.",
            "human_profile": "still here",
            "project_anchor": "old global anchor",
        }),
        encoding="utf-8",
    )
    leftover_bytes = leftover.read_bytes()
    real_replace = os.replace

    def boom(src, dst):
        if str(dst).endswith(CORE_MEMORY_FILE):
            raise OSError("disk")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk"):
        _service(tmp_path).update_core_memory("human_profile", "upgrade", "replace")
    assert leftover.read_bytes() == leftover_bytes
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".core_memory.*.tmp"))


def test_profile_update_drops_a_leftover_global_anchor_without_touching_project(tmp_path):
    path = _write_legacy(tmp_path, profile="old profile", anchor="old global anchor")
    original = path.read_bytes()
    pid = project_id(tmp_path / "alpha")
    viewed = _service(tmp_path).get_core_memory(pid)
    assert viewed.human_profile == "old profile"
    assert viewed.project_anchor == ""
    assert "old global anchor" not in viewed.render_block()
    assert path.read_bytes() == original
    assert not (tmp_path / "core").exists()

    store = FileCoreMemoryStore(tmp_path)
    store.update_project(pid, lambda record: setattr(record, "project_anchor", "alpha anchor"))
    project_bytes = store.project_path(pid).read_bytes()

    updated, error = _service(tmp_path).update_core_memory("human_profile", "new profile", "replace")
    assert error is None and updated.content == "new profile"
    assert store.project_path(pid).read_bytes() == project_bytes
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["human_profile"] == "new profile"
    assert payload["persona"] == "Kept persona."
    assert "project_anchor" not in payload
    assert "unassigned_project_anchor" not in payload


def test_anchor_update_does_not_rewrite_global_content(tmp_path):
    path = _write_legacy(tmp_path, profile="person", anchor="do not assign me")
    original = path.read_bytes()
    pid = project_id(tmp_path / "alpha")
    updated, error = _service(tmp_path).update_core_memory(
        "project_anchor", "alpha only", "replace", project_id=pid,
    )
    assert error is None and updated.content == "alpha only"
    assert path.read_bytes() == original
    viewed = _service(tmp_path).get_core_memory(pid)
    assert viewed.human_profile == "person"
    assert viewed.project_anchor == "alpha only"
    assert "do not assign me" not in viewed.render_block()
    other = _service(tmp_path).get_core_memory(project_id(tmp_path / "beta"))
    assert other.project_anchor == ""
    assert "do not assign me" not in other.render_block()
    assert "alpha only" not in other.render_block()


def test_missing_project_reads_global_and_rejects_anchor_writes(tmp_path):
    service = _service(tmp_path)
    service.update_core_memory("human_profile", "GLOBAL", "replace")
    updated, error = service.update_core_memory("project_anchor", "NOPE", "replace")
    assert updated is None
    assert error == "There is no project context, so project_anchor cannot be updated"
    viewed = service.get_core_memory("")
    assert viewed.human_profile == "GLOBAL"
    assert viewed.project_anchor_state == "none"
    assert "GLOBAL" in viewed.render_block()
    assert "NOPE" not in viewed.render_block()
    assert not (tmp_path / "core").exists()


def test_project_read_failure_keeps_global_and_does_not_fall_back(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.update_core_memory("human_profile", "GLOBAL", "replace")
    pid = project_id(tmp_path / "alpha")
    other = project_id(tmp_path / "beta")
    service.update_core_memory("project_anchor", "beta secret", "replace", project_id=other)
    broken = FileCoreMemoryStore(tmp_path).project_path(pid)
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{broken", encoding="utf-8")
    original = broken.read_bytes()
    global_bytes = (tmp_path / CORE_MEMORY_FILE).read_bytes()

    viewed = service.get_core_memory(pid)
    assert viewed.human_profile == "GLOBAL"
    assert viewed.project_anchor == ""
    assert viewed.project_anchor_state == "read_error"
    assert "beta secret" not in viewed.render_block()
    assert "could not be read" in viewed.to_dict()["project_anchor_note"]
    assert broken.read_bytes() == original
    assert (tmp_path / CORE_MEMORY_FILE).read_bytes() == global_bytes

    def boom(self, project_id):
        raise CoreMemoryStoreError("unavailable")

    monkeypatch.setattr(FileCoreMemoryStore, "load_project", boom)
    manager = assemble_memory_manager(EmptySelectorLLM(), directory=tmp_path)
    manager.bind_project(tmp_path / "alpha")
    prompt = manager.project_system_prompt("Role stays.")
    assert "GLOBAL" in prompt
    assert "beta secret" not in prompt
    assert "Role stays." in prompt
    assert prompt.count("<CORE_MEMORY>") == 1


class _SlowPolicy(CoreMemoryPolicy):
    def compose_section(self, **kwargs):
        import time

        time.sleep(0.02)
        return super().compose_section(**kwargs)


def _append_profile(directory: str, index: int) -> str:
    updated, error = _service(Path(directory), _SlowPolicy()).update_core_memory(
        "human_profile", f"entry_{index}", "append",
    )
    assert error is None and updated is not None
    return updated.content


def _append_anchor(directory: str, project: str, index: int) -> str:
    updated, error = _service(Path(directory), _SlowPolicy()).update_core_memory(
        "project_anchor", f"entry_{index}", "append", project_id=project,
    )
    assert error is None and updated is not None
    return updated.content


def _replace_profile(directory: str, text: str) -> str:
    updated, error = _service(Path(directory), _SlowPolicy()).update_core_memory(
        "human_profile", text, "replace",
    )
    assert error is None and updated is not None
    return updated.content


def _expected_length(prefix: str, indexes) -> int:
    return len(prefix) + sum(len(f"\n- entry_{index}") for index in indexes)


@pytest.mark.parametrize("processes", [False, True], ids=["threads", "processes"])
def test_concurrent_profile_appends_keep_the_whole_section(tmp_path, processes):
    service = _service(tmp_path)
    service.update_core_memory("human_profile", "profile", "replace")
    indexes = range(4)
    _run_pool(
        processes,
        [_append_profile for _ in indexes],
        [(str(tmp_path), index) for index in indexes],
    )
    text = FileCoreMemoryStore(tmp_path).load_global().human_profile
    assert text.startswith("profile")
    for index in indexes:
        assert text.count(f"entry_{index}") == 1
    assert len(text) == _expected_length("profile", indexes)
    assert not (tmp_path / "core").exists()


@pytest.mark.parametrize("processes", [False, True], ids=["threads", "processes"])
def test_concurrent_anchor_appends_keep_the_whole_section(tmp_path, processes):
    pid = project_id(tmp_path / "alpha")
    service = _service(tmp_path)
    service.update_core_memory("human_profile", "global profile", "replace")
    service.update_core_memory("project_anchor", "anchor", "replace", project_id=pid)
    global_bytes = (tmp_path / CORE_MEMORY_FILE).read_bytes()
    indexes = range(4)
    _run_pool(
        processes,
        [_append_anchor for _ in indexes],
        [(str(tmp_path), pid, index) for index in indexes],
    )
    text = FileCoreMemoryStore(tmp_path).load_project(pid).project_anchor
    assert text.startswith("anchor")
    for index in indexes:
        assert text.count(f"entry_{index}") == 1
    assert len(text) == _expected_length("anchor", indexes)
    assert (tmp_path / CORE_MEMORY_FILE).read_bytes() == global_bytes


@pytest.mark.parametrize("processes", [False, True], ids=["threads", "processes"])
def test_concurrent_projects_do_not_overwrite_each_other(tmp_path, processes):
    pid_a = project_id(tmp_path / "alpha")
    pid_b = project_id(tmp_path / "beta")
    service = _service(tmp_path)
    service.update_core_memory("project_anchor", "base-a", "replace", project_id=pid_a)
    service.update_core_memory("project_anchor", "base-b", "replace", project_id=pid_b)
    service.update_core_memory("human_profile", "shared", "replace")
    global_bytes = (tmp_path / CORE_MEMORY_FILE).read_bytes()
    calls = []
    args = []
    for index in range(4):
        calls.append(_append_anchor)
        args.append((str(tmp_path), pid_a if index % 2 == 0 else pid_b, index))
    _run_pool(processes, calls, args)
    store = FileCoreMemoryStore(tmp_path)
    anchor_a = store.load_project(pid_a).project_anchor
    anchor_b = store.load_project(pid_b).project_anchor
    assert anchor_a.startswith("base-a")
    assert anchor_b.startswith("base-b")
    even = [0, 2]
    odd = [1, 3]
    for index in even:
        assert anchor_a.count(f"entry_{index}") == 1
        assert f"entry_{index}" not in anchor_b
    for index in odd:
        assert anchor_b.count(f"entry_{index}") == 1
        assert f"entry_{index}" not in anchor_a
    assert len(anchor_a) == _expected_length("base-a", even)
    assert len(anchor_b) == _expected_length("base-b", odd)
    assert (tmp_path / CORE_MEMORY_FILE).read_bytes() == global_bytes
    assert "shared" in store.load_global().human_profile


def test_concurrent_replace_keeps_one_complete_value(tmp_path):
    _service(tmp_path).update_core_memory("human_profile", "original", "replace")
    texts = ["ALPHA_VALUE", "BETA_VALUE"]
    _run_pool(True, [_replace_profile, _replace_profile], [(str(tmp_path), text) for text in texts])
    saved = FileCoreMemoryStore(tmp_path).load_global().human_profile
    assert saved in texts
    assert saved.count("VALUE") == 1


def _run_pool(processes: bool, functions, arg_sets) -> None:
    executor = (
        ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn"))
        if processes else ThreadPoolExecutor(max_workers=4)
    )
    with executor:
        futures = [
            executor.submit(function, *args)
            for function, args in zip(functions, arg_sets, strict=True)
        ]
        for future in futures:
            future.result(timeout=60)


def _execute(tool, arguments: dict):
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
    return ConcurrentToolExecutor(journal=None).execute_batch([(0, prepared)])[0].result


def test_rebind_switches_tool_writes_and_prompt(tmp_path):
    memory = tmp_path / "memory"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    manager = assemble_memory_manager(EmptySelectorLLM(), directory=memory)
    tools = memory_tools(manager)
    update = next(tool for tool in tools if tool.name == "update_core_memory")
    read = next(tool for tool in tools if tool.name == "get_core_memory")

    failed = _execute(update, {"section": "project_anchor", "content": "too early", "mode": "replace"})
    assert failed.ok is False
    assert "no project context" in failed.err

    manager.bind_project(alpha)
    assert manager.current_project_id == project_id(alpha)
    saved = _execute(update, {
        "section": "project_anchor",
        "content": "ANCHOR_A",
        "mode": "replace",
        "project_id": project_id(beta),
    })
    assert saved.ok is True
    assert saved.data["project_id"] == project_id(alpha)
    assert saved.data["content"] == "ANCHOR_A"
    assert saved.data["scope"] == "current_project"

    manager.bind_project(beta)
    assert manager.current_project_id == project_id(beta)
    saved = _execute(update, {"section": "project_anchor", "content": "ANCHOR_B", "mode": "replace"})
    assert saved.ok and saved.data["project_id"] == project_id(beta)
    profile = _execute(update, {"section": "human_profile", "content": "SHARED", "mode": "replace"})
    assert profile.ok and profile.data["scope"] == "global"

    viewed = _execute(read, {})
    assert viewed.ok
    assert viewed.data["project_anchor"] == "ANCHOR_B"
    assert viewed.data["human_profile"] == "SHARED"
    assert viewed.data["project_id"] == project_id(beta)
    assert "unassigned_project_anchor" not in viewed.data

    prompt_b = manager.project_system_prompt("Role.")
    assert "ANCHOR_B" in prompt_b
    assert "ANCHOR_A" not in prompt_b
    assert "SHARED" in prompt_b
    assert prompt_b.count("<CORE_MEMORY>") == 1

    other = assemble_memory_manager(EmptySelectorLLM(), directory=memory)
    other.bind_project(alpha)
    prompt_a = other.project_system_prompt("Role.")
    assert "ANCHOR_A" in prompt_a
    assert "ANCHOR_B" not in prompt_a
    assert "SHARED" in prompt_a
    assert other.current_project_id != manager.current_project_id


def test_worktree_uses_stable_project_root_not_cwd(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    stable = tmp_path / "stable"
    worktree = tmp_path / "worktree"
    elsewhere = tmp_path / "elsewhere"
    for path in (stable, worktree, elsewhere):
        path.mkdir()
    monkeypatch.chdir(elsewhere)
    manager = assemble_memory_manager(EmptySelectorLLM(), directory=memory)
    session = Session.create("task", worktree, project_root=stable)
    agent = create_agent(
        _DoneLLM(),
        memory_tools(manager),
        session,
        memory=manager,
    )
    assert manager.current_project_id == project_id(stable)
    assert manager.current_project_id != project_id(worktree)
    assert agent.run("hello") == "done"
    tool = next(tool for tool in memory_tools(manager) if tool.name == "update_core_memory")
    result = _execute(tool, {"section": "project_anchor", "content": "STABLE", "mode": "replace"})
    assert result.ok and result.data["project_id"] == project_id(stable)
    assert not FileCoreMemoryStore(memory).project_path(project_id(worktree)).exists()


class _DoneLLM:
    context_limit = 128_000

    def __init__(self):
        self.requests = []

    def __call__(self, request, **kwargs):
        self.requests.append(request.copied_messages())
        yield response(content="done")


class _UpdatingLLM:
    context_limit = 128_000

    def __init__(self):
        self.requests = []

    def __call__(self, request, **kwargs):
        self.requests.append(request.copied_messages())
        if len(self.requests) == 1:
            yield response(calls=[{
                "name": "update_core_memory",
                "arguments": {
                    "section": "project_anchor",
                    "content": "ANCHOR_A",
                    "mode": "replace",
                },
            }])
        else:
            yield response(content="done")


@pytest.mark.parametrize("legacy", [False, True], ids=["new-session", "legacy-session"])
def test_tool_update_refreshes_next_request_and_preserves_history(tmp_path, legacy):
    class UpdatingLLM:
        context_limit = 128_000

        def __init__(self):
            self.requests = []

        def __call__(self, request, **kwargs):
            self.requests.append(request.copied_messages())
            if len(self.requests) == 1:
                yield response(calls=[{
                    "name": "update_core_memory",
                    "arguments": {
                        "section": "human_profile",
                        "content": "NEW_PROFILE",
                        "mode": "replace",
                    },
                }])
            else:
                yield response(content="done")

    llm = UpdatingLLM()
    manager = assemble_memory_manager(llm, selector_llm=EmptySelectorLLM(), directory=tmp_path)
    manager.service.update_core_memory("human_profile", "OLD_PROFILE", "replace")
    session = Session.create("memory update", tmp_path)
    session.active_deferred_tools = ["update_core_memory"]
    if legacy:
        session.append_message({
            "role": "system",
            "content": manager.service.get_core_memory().render_block() + "\n\nPreserve this role.",
        })
    agent = create_agent(
        llm, memory_tools(manager), session, memory=manager,
        permission_resolver=PermissionResolver(
            approval_handler=lambda _: PermissionResponse("allow_once"),
        ),
    )
    original_system = deepcopy(session.message_records[0].message)
    assert agent.run("Update my profile") == "done"
    assert len(llm.requests) == 2
    assert "OLD_PROFILE" in llm.requests[0][0]["content"]
    next_prompt = llm.requests[1][0]["content"]
    assert "NEW_PROFILE" in next_prompt
    assert "OLD_PROFILE" not in next_prompt
    assert next_prompt.count("<CORE_MEMORY>") == 1
    assert session.message_records[0].message == original_system
    if legacy:
        assert "Preserve this role." in next_prompt
    else:
        assert "<CORE_MEMORY>" not in original_system["content"]

    _service(tmp_path).update_core_memory("human_profile", "EXTERNAL_PROFILE", "replace")
    assert agent.run("Read my current profile") == "done"
    assert "EXTERNAL_PROFILE" in llm.requests[-1][0]["content"]
    assert "NEW_PROFILE" not in llm.requests[-1][0]["content"]
    assert session.message_records[0].message == original_system


def test_anchor_update_is_on_the_next_request_and_not_another_project(tmp_path):
    memory = tmp_path / "memory"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    llm = _UpdatingLLM()
    manager = assemble_memory_manager(llm, selector_llm=EmptySelectorLLM(), directory=memory)
    session = Session.create("anchor", alpha)
    session.active_deferred_tools = ["update_core_memory"]
    session.append_message({
        "role": "system",
        "content": (
            "<CORE_MEMORY>\n- Project Core Anchor: FOREIGN_ANCHOR\n</CORE_MEMORY>\n\nKeep the role."
        ),
    })
    agent = create_agent(
        llm,
        memory_tools(manager),
        session,
        memory=manager,
        permission_resolver=PermissionResolver(
            approval_handler=lambda _: PermissionResponse("allow_once"),
        ),
    )
    original_system = deepcopy(session.message_records[0].message)
    assert agent.run("Remember the project rule") == "done"
    first = llm.requests[0][0]["content"]
    second = llm.requests[1][0]["content"]
    assert "FOREIGN_ANCHOR" not in first
    assert "ANCHOR_A" not in first
    assert "Keep the role." in first
    assert first.count("<CORE_MEMORY>") == 1
    assert "ANCHOR_A" in second
    assert "FOREIGN_ANCHOR" not in second
    assert second.count("<CORE_MEMORY>") == 1
    assert session.message_records[0].message == original_system

    other = assemble_memory_manager(EmptySelectorLLM(), directory=memory)
    other.bind_project(beta)
    prompt = other.project_system_prompt("Other role.")
    assert "ANCHOR_A" not in prompt
    assert "FOREIGN_ANCHOR" not in prompt
    assert manager.project_system_prompt("Same role.") .count("ANCHOR_A") == 1


def test_core_read_failure_does_not_block_delivery(tmp_path, monkeypatch):
    manager = assemble_memory_manager(EmptySelectorLLM(), directory=tmp_path)
    session = Session.create("deliver", tmp_path / "proj")

    def boom(self):
        raise RuntimeError("disk")

    monkeypatch.setattr(FileCoreMemoryStore, "load_global", boom)
    llm = _DoneLLM()
    agent = create_agent(llm, memory_tools(manager), session, memory=manager)
    assert agent.run("ship it") == "done"
    assert llm.requests
    assert "<CORE_MEMORY>" not in llm.requests[0][0]["content"]
