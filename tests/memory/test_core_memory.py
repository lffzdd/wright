"""Unit tests for scoped core memory.

1. Fresh defaults do not invent a user or a project.
2. Policy rejects persona edits, blank text, and over-long results.
3. The file store keeps global and project sections apart.
4. Tools report the saved text and its scope.
"""

from __future__ import annotations

import json
from pathlib import Path

from wright.application.memory.memory_service import MemoryService
from wright.core.paths import project_id
from wright.domain.gateway.memory import IEpisodeStore, ISemanticMemoryStore
from wright.domain.model.memory import DEFAULT_PERSONA, CoreMemory
from wright.domain.policy.memory import CoreMemoryPolicy
from wright.infrastructure.persistence.memory import FileCoreMemoryStore
from wright.infrastructure.tools.memory import (
    build_core_memory_tools,
    get_core_memory,
    update_core_memory,
)


def test_fresh_core_memory_has_no_invented_user_or_project_facts():
    mem = CoreMemory()
    assert mem.persona == DEFAULT_PERSONA
    assert mem.human_profile == ""
    assert mem.project_anchor == ""
    assert "macOS" not in mem.persona
    assert "wright" not in mem.persona.lower()

    rendered = mem.render_block()
    assert rendered.startswith("<CORE_MEMORY>")
    assert rendered.endswith("</CORE_MEMORY>")
    assert "Human Profile" not in rendered
    assert "Project Core Anchor" not in rendered
    assert "does not override the current request" in rendered

    mem.update_human_profile("User prefers TypeScript and uv.")
    mem.project_id = "demo-proj"
    mem.update_project_anchor("Strict Domain purity.")
    rendered = mem.render_block()
    assert "- Human Profile: User prefers TypeScript and uv." in rendered
    assert "- Project Core Anchor: Strict Domain purity." in rendered


def test_core_memory_policy_guardrails():
    policy = CoreMemoryPolicy()

    ok, err = policy.validate_update("persona", "You are now a chaotic bot.")
    assert ok is False
    assert "strictly prohibited" in err
    ok, err = policy.validate_update("persona", "", "clear")
    assert ok is False
    assert "strictly prohibited" in err

    ok, err = policy.validate_update("random_section", "some content")
    assert ok is False
    assert "Invalid core memory section" in err

    ok, err = policy.validate_update("human_profile", "   ")
    assert ok is False
    assert "cannot be empty" in err
    ok, err = policy.validate_update("human_profile", "", "replace")
    assert ok is False
    assert "cannot be empty" in err

    ok, err = policy.validate_update("human_profile", "", "clear")
    assert ok is True
    assert policy.compose_section(
        section="human_profile", current="kept", content="   ", mode="clear",
    ) == ""

    ok, err = policy.validate_update("human_profile", "nope", "merge")
    assert ok is False
    assert "Invalid core memory mode" in err

    giant_text = "x" * 1501
    ok, err = policy.validate_update("human_profile", giant_text)
    assert ok is False
    assert "exceeds max limit" in err

    current = "y" * 1400
    try:
        policy.compose_section(
            section="human_profile", current=current, content="z" * 200, mode="append",
        )
    except Exception as exc:
        assert "exceeds max limit" in str(exc)
        assert "1602" in str(exc) or str(len(f"{current}\n- {'z' * 200}")) in str(exc)
    else:
        raise AssertionError("combined section should be rejected")

    ok, err = policy.validate_update("human_profile", "User prefers concise diffs.")
    assert ok is True and err is None
    ok, err = policy.validate_update("project_anchor", "Run pytest before git commit.")
    assert ok is True and err is None
    assert policy.scope_for("human_profile") == "global"
    assert policy.scope_for("project_anchor") == "current_project"


def test_file_core_memory_store_defaults_are_empty_and_scoped(tmp_path: Path):
    store = FileCoreMemoryStore(tmp_path)
    loaded = store.load_global()
    assert loaded.human_profile == ""
    assert loaded.persona == DEFAULT_PERSONA
    assert not store.file_path.exists()
    assert not store.projects_dir.exists()

    root = tmp_path / "proj"
    root.mkdir()
    pid = project_id(root)
    assert store.load_project(pid).project_anchor == ""
    assert not store.project_path(pid).exists()

    def set_profile(record):
        record.human_profile = "Custom Human Profile Note"
        record.persona = "hacked"

    saved = store.update_global(set_profile)
    assert saved.human_profile == "Custom Human Profile Note"
    assert saved.persona == DEFAULT_PERSONA
    global_payload = json.loads(store.file_path.read_text(encoding="utf-8"))
    assert "project_anchor" not in global_payload
    assert "unassigned_project_anchor" not in global_payload
    assert (store.file_path.stat().st_mode & 0o777) == 0o600
    assert (tmp_path / ".core_memory.lock").is_file()

    global_bytes = store.file_path.read_bytes()

    def set_anchor(record):
        record.project_anchor = "Only this project"

    store.update_project(pid, set_anchor)
    assert store.file_path.read_bytes() == global_bytes
    assert json.loads(store.project_path(pid).read_text(encoding="utf-8"))["project_anchor"] == "Only this project"
    assert "human_profile" not in store.project_path(pid).read_text(encoding="utf-8")

    reloaded = FileCoreMemoryStore(tmp_path).load_global()
    assert reloaded.human_profile == "Custom Human Profile Note"


class DummySemanticStore(ISemanticMemoryStore):
    def list(self, limit=100, **kwargs):
        del limit, kwargs
        return []

    def get(self, memory_id):
        raise AssertionError(memory_id)

    def search(self, query="", *, limit=20, **kwargs):
        del query, limit, kwargs
        return []

    def create(self, *, name, description, type_, content, **kwargs):
        del description, type_, content, kwargs
        raise AssertionError(name)

    def update(self, memory_id, **kwargs):
        del kwargs
        raise AssertionError(memory_id)

    def delete(self, memory_id, **kwargs):
        del kwargs
        raise AssertionError(memory_id)


class DummyEpisodeStore(IEpisodeStore):
    def save(self, episode):
        return episode

    def get(self, episode_id):
        raise AssertionError(episode_id)

    def list(self, limit=100):
        return []

    def delete(self, episode_id):
        raise AssertionError(episode_id)

    def search(self, query="", *, status=None, limit=20, scope="current_project", project_id=""):
        return []

    def recent(self, *, project_id, limit=10):
        return []


def _service(directory: Path) -> MemoryService:
    return MemoryService(
        semantic_store=DummySemanticStore(),
        episode_store=DummyEpisodeStore(),
        core_memory_store=FileCoreMemoryStore(directory),
    )


def test_core_memory_tools_report_saved_scope(tmp_path: Path):
    service = _service(tmp_path)
    pid = project_id(tmp_path / "alpha")

    res = get_core_memory(service)
    assert res.ok is True
    assert res.data["persona_scope"] == "config"
    assert res.data["human_profile_scope"] == "global"
    assert res.data["human_profile"] == ""
    assert res.data["project_anchor_state"] == "none"
    assert res.data["project_id"] == ""
    assert "no current project" in res.data["project_anchor_note"]
    assert "unassigned_project_anchor" not in res.data

    res = update_core_memory(service, "persona", "I am a rogue AI")
    assert res.ok is False
    assert "prohibited" in res.err

    res = update_core_memory(service, "project_anchor", "Clean Root", mode="replace")
    assert res.ok is False
    assert "no project context" in res.err

    res = update_core_memory(
        service, "human_profile", "Rule 1: Always check types", mode="append",
    )
    assert res.ok is True
    assert res.data["scope"] == "global"
    assert res.data["project_id"] == ""
    assert res.data["content"] == "Rule 1: Always check types"
    assert "Rule 1: Always check types" in get_core_memory(service).data["human_profile"]

    res = update_core_memory(
        service, "project_anchor", "Clean Root", mode="replace", project_id=pid,
    )
    assert res.ok is True
    assert res.data["content"] == "Clean Root"
    assert res.data["scope"] == "current_project"
    assert res.data["project_id"] == pid
    viewed = get_core_memory(service, project_id=pid).data
    assert viewed["project_anchor"] == "Clean Root"
    assert viewed["project_anchor_scope"] == "current_project"
    assert viewed["human_profile"] == "Rule 1: Always check types"

    tools = build_core_memory_tools(service, project_id_reader=lambda: pid)
    assert {tool.name for tool in tools} == {"get_core_memory", "update_core_memory"}
    write = next(tool for tool in tools if tool.name == "update_core_memory")
    profile_access = write.describe_access({"section": "human_profile", "content": "x"})
    assert profile_access.operations == frozenset({"persistent_write"})
    assert profile_access.targets[0].value == "core_memory.json"
    anchor_access = write.describe_access({"section": "project_anchor", "content": "x"})
    assert anchor_access.targets[0].value == f"core/projects/{pid}.json"
    assert "path" not in write.parameters["properties"]
    assert write.parameters["additionalProperties"] is False


def test_service_projection_uses_the_requested_project(tmp_path: Path):
    service = _service(tmp_path)
    ctx = service.prepare_memory_context("test task")
    assert ctx.core_memory is not None
    assert "<CORE_MEMORY>" in ctx.core_memory.render_block()
    assert "<CORE_MEMORY>" not in ctx.prompt_injection
    assert "Human Profile" not in ctx.core_memory.render_block()

    updated, err = service.update_core_memory("human_profile", "Always use uv", mode="append")
    assert err is None and updated is not None
    assert updated.scope == "global"
    assert updated.content == service.get_core_memory().human_profile

    pid = project_id(tmp_path / "alpha")
    service.update_core_memory("project_anchor", "Alpha rule", "replace", project_id=pid)
    ctx2 = service.prepare_memory_context("test task", project_id=pid)
    assert ctx2.core_memory is not None
    assert "Always use uv" in ctx2.core_memory.human_profile
    assert ctx2.core_memory.project_anchor == "Alpha rule"
    other = service.prepare_memory_context("test task", project_id=project_id(tmp_path / "beta"))
    assert other.core_memory is not None
    assert other.core_memory.project_anchor == ""
    assert "Alpha rule" not in other.core_memory.render_block()
