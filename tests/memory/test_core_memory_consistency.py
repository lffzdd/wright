"""Regression coverage for core memory request freshness and atomic updates."""

from __future__ import annotations

import json
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from tests.memory.test_agent_integration import EmptySelectorLLM
from tests.memory.test_core_memory import DummyEpisodeStore, DummySemanticStore
from tests.responses import response
from wright.application.agent import create_agent
from wright.application.memory import MemoryManager, MemoryService
from wright.domain.model.memory import CoreMemory
from wright.domain.model.session import Session
from wright.domain.policy import PermissionResolver, PermissionResponse
from wright.domain.policy.memory import CoreMemoryPolicy
from wright.infrastructure.persistence.memory import FileCoreMemoryStore
from wright.interfaces.renderer import SilentRenderer


def _service(directory: Path, policy: CoreMemoryPolicy | None = None) -> MemoryService:
    return MemoryService(
        DummySemanticStore(), DummyEpisodeStore(),
        core_memory_store=FileCoreMemoryStore(directory),
        core_memory_policy=policy,
    )


@pytest.mark.parametrize("mode", ["apend", "REPLACE", "", None])
def test_invalid_mode_rejected_without_touching_storage(tmp_path, mode):
    service = _service(tmp_path)
    updated, error = service.update_core_memory("human_profile", "replacement", mode)
    assert updated is None
    assert "Invalid core memory mode" in error
    assert not list(tmp_path.iterdir())

    service.update_core_memory("human_profile", "preserved", "replace")
    memory_file = tmp_path / "core_memory.json"
    original = memory_file.read_bytes()
    updated, error = service.update_core_memory("human_profile", "replacement", mode)
    assert updated is None
    assert memory_file.read_bytes() == original


def test_rejected_append_preserves_file_and_releases_lock(tmp_path):
    service = _service(tmp_path)
    service.update_core_memory("human_profile", "x" * 1500, "replace")
    memory_file = tmp_path / "core_memory.json"
    original = memory_file.read_bytes()

    updated, error = service.update_core_memory("human_profile", "overflow", "append")
    assert updated is None
    assert "exceeds max limit" in error
    assert memory_file.read_bytes() == original

    updated, error = _service(tmp_path).update_core_memory("human_profile", "recovered", "replace")
    assert error is None
    assert updated.content == "recovered"


def test_failed_mutation_and_corrupt_storage_are_not_overwritten(tmp_path):
    store = FileCoreMemoryStore(tmp_path)
    store.save(CoreMemory(human_profile="preserved"))
    original = store.file_path.read_bytes()

    def fail_after_mutation(memory):
        memory.update_human_profile("not committed")
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        store.update(fail_after_mutation)
    assert store.file_path.read_bytes() == original

    store.file_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _service(tmp_path).update_core_memory("human_profile", "new", "append")
    assert store.file_path.read_text(encoding="utf-8") == "{broken"


class _SlowPolicy(CoreMemoryPolicy):
    def apply_update(self, memory, section, content, mode="append"):
        # Widen the read/write window while another client tries to update.
        time.sleep(0.03)
        super().apply_update(memory, section, content, mode)


def _append_from_independent_client(directory: Path, index: int):
    section = "human_profile" if index % 2 == 0 else "project_anchor"
    updated, error = _service(directory, _SlowPolicy()).update_core_memory(
        section, f"entry_{index}", "append",
    )
    assert error is None
    assert updated is not None
    return updated


@pytest.mark.parametrize("processes", [False, True], ids=["threads", "processes"])
def test_concurrent_updates_preserve_all_appends_and_both_sections(tmp_path, processes):
    store = FileCoreMemoryStore(tmp_path)
    store.save(CoreMemory(human_profile="profile", project_anchor="anchor"))
    executor = (
        ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn"))
        if processes else ThreadPoolExecutor(max_workers=4)
    )
    with executor:
        futures = [
            executor.submit(_append_from_independent_client, tmp_path, index)
            for index in range(8)
        ]
        for future in futures:
            future.result(timeout=30)

    memory = store.load()
    assert memory.human_profile.startswith("profile")
    assert memory.project_anchor.startswith("anchor")
    for index in range(8):
        content = memory.human_profile if index % 2 == 0 else memory.project_anchor
        assert content.count(f"entry_{index}") == 1


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
                        "section": "human_profile", "content": "NEW_PROFILE", "mode": "replace",
                    },
                }])
            else:
                yield response(content="done")

    llm = UpdatingLLM()
    manager = MemoryManager(llm, selector_llm=EmptySelectorLLM(), directory=tmp_path)
    manager.service.update_core_memory("human_profile", "OLD_PROFILE", "replace")
    session = Session.create("memory update", tmp_path)
    session.active_deferred_tools = ["update_core_memory"]
    if legacy:
        session.append_message({
            "role": "system",
            "content": manager.service.get_core_memory().render_block() + "\n\nPreserve this role.",
        })
    agent = create_agent(
        llm, manager.tools(), session, SilentRenderer(), memory=manager,
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

    # A different session/process can update memory between user turns too.
    _service(tmp_path).update_core_memory("human_profile", "EXTERNAL_PROFILE", "replace")
    assert agent.run("Read my current profile") == "done"
    assert "EXTERNAL_PROFILE" in llm.requests[-1][0]["content"]
    assert "NEW_PROFILE" not in llm.requests[-1][0]["content"]
    assert session.message_records[0].message == original_system
