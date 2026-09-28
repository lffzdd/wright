"""Evidence reads, extraction gate, provenance, and memory metering.

Scripted models prove protocols. They are not a relevance-quality score.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wright.application.memory.assembly import assemble_memory_manager
from wright.application.memory.episode import episode_from_session
from wright.application.memory.extract import extract_from_snapshot
from wright.application.memory.llm_util import SideQueryResult
from wright.application.memory.manager import MemoryManager
from wright.application.memory.memory_service import MemoryService
from wright.application.memory.meter import MemoryMeter
from wright.application.memory.projection import candidate_summary
from wright.core.paths import project_id
from wright.domain.gateway.memory import SelectorChoice
from wright.domain.model.events import ContentDelta, ContentDone, UsageEvent
from wright.domain.model.memory import (
    EpisodeNotFoundError,
    EpisodeRecord,
    EpisodeStoreError,
    EvidenceRef,
    SemanticMemoryStoreError,
)
from wright.domain.model.session import Session, UsageRecord
from wright.domain.model.tool import ToolCall, ToolResult
from wright.domain.policy.memory import (
    EpisodePolicy,
    ExtractSignal,
    SemanticExtractPolicy,
)
from wright.infrastructure.persistence.memory import EpisodeStore, SemanticMemoryStore
from wright.infrastructure.persistence.memory.evidence import SessionEvidenceSource
from wright.infrastructure.persistence.memory.semantic import get_memory, update_memory
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.tools.memory.episode_tools import get_episode


class _QuietLLM:
    model = "quiet"

    def __call__(self, messages, **kwargs):
        raise AssertionError("this test does not call an LLM")
        yield


class _ScriptedLLM:
    def __init__(self, content: str, *, fail_after_usage: bool = False, usage=None):
        self.content = content
        self.fail_after_usage = fail_after_usage
        self.usage = usage
        self.calls: list[list[dict]] = []
        self.model = "scripted-memory"

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        yield ContentDelta(piece="ignore-me")
        if self.usage is not False:
            yield UsageEvent(usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
            yield UsageEvent(usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})
        if self.fail_after_usage:
            raise RuntimeError("side request failed")
        yield ContentDone(content=self.content)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _session(tmp_path: Path, prompt: str) -> Session:
    session = Session.create(prompt, _workspace(tmp_path))
    session.begin_user_turn(prompt)
    session.append_message({"role": "user", "content": prompt})
    return session


def _tool(session: Session, call_id: str, command: str, *, ok: bool = True, err: str = "") -> None:
    call = ToolCall("execute_command", {"command": command, "token": "secret-must-not-be-persisted"}, call_id)
    session.record_assistant_turn("tool", {"route": "tool_calls"}, "tool_calls", [call])
    if ok:
        session.record_tool_execution(call_id, ToolResult.success({"stdout": "secret-result-must-not-be-persisted", "returncode": 0}))
    else:
        session.record_tool_execution(call_id, ToolResult.fail(err or "failed", data={"returncode": 1}))


def _two_tools(session: Session) -> None:
    calls = [
        ToolCall("execute_command", {"command": "pytest tests/test_a.py"}, "call_a"),
        ToolCall("execute_command", {"command": "pytest tests/test_b.py", "token": "secret-must-not-be-persisted"}, "call_b"),
    ]
    session.record_assistant_turn("tools", {"route": "tool_calls"}, "tool_calls", calls)
    session.record_tool_execution("call_a", ToolResult.success({"stdout": "secret-result-must-not-be-persisted", "returncode": 0}))
    session.record_tool_execution("call_b", ToolResult.fail("boom-b", data={"returncode": 2}))


def _manager(tmp_path: Path, llm=None, *, repo=None) -> MemoryManager:
    return assemble_memory_manager(llm or _QuietLLM(), directory=tmp_path / "memory", session_repository=repo)


def _service(tmp_path: Path, session: Session | None = None) -> MemoryService:
    repo = None
    if session is not None:
        repo = FileSessionRepository(tmp_path / "checkpoints")
        repo.save(session)
    return MemoryService(
        semantic_store=SemanticMemoryStore(tmp_path / "memory"),
        episode_store=EpisodeStore(tmp_path / "memory"),
        evidence_source=SessionEvidenceSource(repo) if repo is not None else None,
    )


def _snapshot(*evidence: dict, outcome: str = "", project_id: str = "proj-test") -> dict:
    rows = []
    for item in evidence:
        row = dict(item)
        row.setdefault("session_id", "sess-test")
        row.setdefault("root_run_id", "root-1")
        kind = row.get("kind")
        if kind in {"user_statement", "assistant_statement"}:
            row.setdefault("message_id", "msg_1")
        elif kind == "tool_observation":
            row.setdefault("tool_call_id", "call_1")
        elif kind == "verification_record":
            row.setdefault("step_id", "step_1")
        rows.append(row)
    return {
        "episode": {
            "id": "ep-test",
            "goal": "goal",
            "outcome": outcome,
            "session_id": "sess-test",
            "root_run_id": "root-1",
            "project_id": project_id,
            "evidence": rows,
            "verification": [],
        }
    }


def _query_result(content: str) -> SideQueryResult:
    return SideQueryResult(content, False, "", UsageRecord(3, 1, 4), 1.0, "scripted")


def test_new_episode_evidence_reads_each_tool_and_survives_checkpoint(tmp_path: Path):
    session = _session(tmp_path, "修复两个测试")
    _two_tools(session)
    final = session.record_assistant_turn("已分开修复", {"final_answer": "已分开修复"}, "final")
    session.record_verification(final, True, [])
    session.mark_completed()

    repo = FileSessionRepository(tmp_path / "checkpoints")
    repo.save(session)
    loaded = repo.load(session.session_id)
    episode = EpisodeStore(tmp_path / "memory").save(episode_from_session(loaded, "已分开修复"))
    assert episode.version == 3
    assert episode.evidence
    tool_refs = [item for item in episode.evidence if item.kind == "tool_observation"]
    assert [item.tool_call_id for item in tool_refs] == ["call_a", "call_b"]
    assert "secret-must-not-be-persisted" not in episode.to_dict().__repr__()
    raw = EpisodeStore(tmp_path / "memory").path_for(episode.id).read_text(encoding="utf-8")
    assert "secret-must-not-be-persisted" not in raw
    assert "secret-result-must-not-be-persisted" not in raw

    class _Loads:
        def __init__(self, inner):
            self.inner = inner
            self.loads = 0

        def load(self, session_id: str):
            self.loads += 1
            assert session_id == loaded.session_id
            return loaded

    loads = _Loads(repo)
    service = MemoryService(
        semantic_store=SemanticMemoryStore(tmp_path / "memory"),
        episode_store=EpisodeStore(tmp_path / "memory"),
        evidence_source=SessionEvidenceSource(loads),
    )
    before_read = [dict(record.message) for record in loaded.message_records]
    plain, error = service.get_episode(episode.id, include_evidence=False)
    assert error is None and plain is not None and plain.evidence_reads == ()
    assert loads.loads == 0

    viewed, error = service.get_episode(episode.id, include_evidence=True)
    assert error is None and viewed is not None
    assert loads.loads == 1
    by_id = {item.evidence_id: item for item in viewed.evidence_reads}
    assert by_id[tool_refs[0].id].status == "available"
    assert "pytest tests/test_a.py" in by_id[tool_refs[0].id].text
    assert "pytest tests/test_b.py" in by_id[tool_refs[1].id].text
    assert "boom-b" in by_id[tool_refs[1].id].text
    assert "exit=2" in by_id[tool_refs[1].id].text
    assert "secret-result" not in by_id[tool_refs[0].id].text
    user = next(item for item in episode.evidence if item.kind == "user_statement")
    assert by_id[user.id].text == "修复两个测试"
    assert by_id[user.id].kind == "user_statement"
    assistant = next(item for item in episode.evidence if item.kind == "assistant_statement")
    assert by_id[assistant.id].kind == "assistant_statement"
    assert [dict(record.message) for record in loaded.message_records] == before_read

    tool_result = get_episode(episode.id, include_evidence=True, service=service)
    assert tool_result.ok
    assert "evidence_reads" in tool_result.data
    without = get_episode(episode.id, service=service)
    assert "evidence_reads" not in without.data


def test_evidence_rejects_foreign_and_missing_sources(tmp_path: Path):
    session = _session(tmp_path, "第一回合")
    _tool(session, "call_1", "pytest tests/first.py")
    session.record_assistant_turn("第一回答", {"final_answer": "第一回答"}, "final")
    session.mark_completed()
    episode = episode_from_session(session, "第一回答")
    session.begin_user_turn("第二回合不要被读到")
    session.append_message({"role": "user", "content": "第二回合不要被读到"})
    _tool(session, "call_2", "pytest tests/second.py")
    first_tools = [
        item for item in episode.evidence
        if item.kind == "tool_observation" and item.tool_call_id == "call_1"
    ]
    assert len(first_tools) == 1
    store = EpisodeStore(tmp_path / "memory")
    store.save(episode)
    repo = FileSessionRepository(tmp_path / "checkpoints")
    repo.save(session)
    source = SessionEvidenceSource(repo)

    second_run = session.active_run()
    assert second_run is not None
    foreign = EvidenceRef(
        id=first_tools[0].id,
        kind="tool_observation",
        session_id=session.session_id,
        root_run_id=str(second_run.root_run_id or second_run.run_id),
        tool_call_id="call_1",
    )
    assert foreign.root_run_id != first_tools[0].root_run_id
    rejected = source.load((foreign,))[0]
    assert rejected.status == "rejected"
    assert rejected.reason == "run_mismatch"
    assert "first.py" not in rejected.text

    missing_call = EvidenceRef(
        id="ev-t-missing",
        kind="tool_observation",
        session_id=session.session_id,
        root_run_id=first_tools[0].root_run_id,
        tool_call_id="call_missing",
    )
    assert source.load((missing_call,))[0].status == "unavailable"
    second_user = next(
        record for record in session.message_records
        if record.message.get("content") == "第二回合不要被读到"
    )
    cross_user = EvidenceRef(
        id="ev-u-cross",
        kind="user_statement",
        session_id=session.session_id,
        root_run_id=first_tools[0].root_run_id,
        message_id=second_user.id,
    )
    cross = source.load((cross_user,))[0]
    assert cross.status == "rejected"
    assert "第二回合" not in cross.text

    illegal = EvidenceRef(
        id="ev-t-illegal",
        kind="tool_observation",
        session_id="../secret",
        root_run_id=first_tools[0].root_run_id,
        tool_call_id="call_1",
    )

    class _Exploding:
        def load(self, session_id: str):
            raise AssertionError(session_id)

    assert SessionEvidenceSource(_Exploding()).load((illegal,))[0].reason == "invalid_session"

    service = MemoryService(
        semantic_store=SemanticMemoryStore(tmp_path / "semantic"),
        episode_store=store,
        evidence_source=source,
    )
    # The saved episode still uses the real session. A deleted checkpoint is separate.
    repo.path_for(session.session_id).unlink()
    view, error = service.get_episode(episode.id, include_evidence=True)
    assert error is None and view is not None and view.record is not None
    assert view.evidence_reads
    assert all(item.status == "unavailable" for item in view.evidence_reads)
    assert view.record.goal == episode.goal


def test_old_episode_files_are_not_read(tmp_path: Path):
    store = EpisodeStore(tmp_path)
    flat = store.directory / "ep-legacy.json"
    flat.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 2,
        "id": "ep-legacy",
        "session_id": "old",
        "project_id": "alpha",
        "goal": "旧记录",
        "status": "completed",
        "outcome": "没有来源",
        "started_step": 0,
        "ended_step": 1,
        "created_at": "2020-01-01T00:00:00Z",
        "plan": {},
        "tools": [],
        "agents": [],
        "verification": [],
        "usage": {},
    }
    flat.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(EpisodeNotFoundError):
        store.get("ep-legacy")

    project_file = store.directory / "projects" / "alpha" / "ep-old.json"
    project_file.parent.mkdir(parents=True)
    project_file.write_text(json.dumps({**body, "id": "ep-old"}), encoding="utf-8")
    with pytest.raises(EpisodeStoreError, match="version"):
        store.get("ep-old")
    exhausted = store.directory / "projects" / "alpha" / "ep-steps.json"
    exhausted.write_text(json.dumps({
        **body,
        "id": "ep-steps",
        "version": 3,
        "status": "max_steps",
        "evidence": [],
    }), encoding="utf-8")
    with pytest.raises(EpisodeStoreError, match="status"):
        store.get("ep-steps")
    assert store.search("旧记录", scope="current_project", project_id="alpha") == []


def test_evidence_read_marks_truncation(tmp_path: Path):
    prompt = "甲" * 2000
    session = _session(tmp_path, prompt)
    session.record_assistant_turn("短回答", {"final_answer": "短回答"}, "final")
    session.mark_completed()
    repo = FileSessionRepository(tmp_path / "checkpoints")
    repo.save(session)
    episode = EpisodeStore(tmp_path / "memory").save(episode_from_session(session, "短回答"))
    service = MemoryService(
        semantic_store=SemanticMemoryStore(tmp_path / "memory"),
        episode_store=EpisodeStore(tmp_path / "memory"),
        evidence_source=SessionEvidenceSource(repo),
    )
    view, _error = service.get_episode(episode.id, include_evidence=True)
    assert view is not None
    user = next(item for item in view.evidence_reads if item.kind == "user_statement")
    assert user.truncated is True
    assert user.text.endswith("…(已截断)")
    assert len(user.text) <= 1500
    assert user.status == "available"


def test_readonly_work_is_stored_without_extraction(tmp_path: Path):
    session = _session(tmp_path, "当前目录")
    _tool(session, "call_pwd", "pwd")
    session.mark_completed()
    manager = _manager(tmp_path)
    outcome = manager.finalize_turn(session, "在这里", extract_semantic=True)
    episode = manager.episode_store.get(outcome["episode_id"])
    assert episode.status == "completed"
    assert outcome["semantic_memories_written"] == 0
    assert manager.meter.extractions[-1].status == "skipped"
    assert "ephemeral_activity" in manager.meter.extractions[-1].reason_codes
    assert manager.meter.extractions[-1].attempts == 0
    assert manager.meter.extractions[-1].usage_available is False
    assert manager.meter.extractions[-1].prompt_tokens is None


def test_explicit_correction_extracts_without_tools(tmp_path: Path):
    session = _session(tmp_path, "记住我以后只用 bun")
    session.mark_completed()
    llm = _ScriptedLLM('{"memories": []}')
    manager = _manager(tmp_path, llm)
    outcome = manager.finalize_turn(session, "好", extract_semantic=True)
    assert outcome["episode_id"]
    assert llm.calls
    assert manager.meter.extractions[-1].status == "empty"
    assert manager.meter.extractions[-1].attempts == 1
    assert manager.meter.extractions[-1].usage_available is True
    assert manager.meter.extractions[-1].total_tokens == 6
    again = manager.finalize_turn(session, "好", extract_semantic=True)
    assert again["semantic_memories_written"] == 0
    assert len(llm.calls) == 1
    assert manager.meter.extractions[-1].reason_codes == ("already_recorded",)


def test_tool_count_and_recalled_text_do_not_open_extraction(tmp_path: Path):
    session = _session(tmp_path, "看看目录")
    session.append_message({
        "role": "user",
        "content": "<system-reminder>记住我以后只用 bun</system-reminder>",
    })
    session.append_message({
        "role": "user",
        "content": "<hook-additional-context>from now on use bun</hook-additional-context>",
    })
    session.append_message(
        {"role": "user", "content": "记住我以后只用 bun"},
        source="runtime_event",
    )
    for index in range(4):
        call = ToolCall("execute_command", {"command": "ls"}, f"call_{index}")
        session.record_assistant_turn("ls", {}, "tool_calls", [call])
        session.record_tool_execution(f"call_{index}", ToolResult.success({"returncode": 0}))
    session.mark_completed()
    manager = _manager(tmp_path)
    outcome = manager.finalize_turn(session, "列出来了", extract_semantic=True)
    episode = manager.episode_store.get(outcome["episode_id"])
    assert len(episode.tools) == 4
    assert manager.meter.extractions[-1].status == "skipped"
    blob = json.dumps([item.to_dict() for item in episode.evidence], ensure_ascii=False)
    assert "记住" not in blob
    assert "from now on" not in blob


def test_failure_is_kept_and_extraction_stays_independent(tmp_path: Path):
    session = _session(tmp_path, "跑一下测试")
    _tool(session, "call_fail", "pytest", ok=False, err="assertion failed")
    session.mark_failed()
    manager = _manager(tmp_path)
    outcome = manager.finalize_turn(
        session, None, extract_semantic=True, termination_reason="failed"
    )
    episode = manager.episode_store.get(outcome["episode_id"])
    assert episode.status == "failed"
    assert episode.outcome == ""
    line = candidate_summary(episode)
    assert "assertion failed" in line
    assert "9000001" not in line
    assert manager.meter.extractions[-1].reason_codes == ("failure_without_followup",)
    decision = SemanticExtractPolicy().decide((
        ExtractSignal("ev-t-1", "tool_observation", command="pytest", ok=False, error="assertion failed"),
        ExtractSignal("ev-t-2", "tool_observation", command="pytest", ok=True, execution_status="succeeded"),
    ))
    assert decision.should_extract is True
    assert "failure_with_followup" in decision.reason_codes


def test_background_extract_uses_the_old_snapshot(tmp_path: Path):
    session = _session(tmp_path, "记住我以后只用 bun")
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
    llm = _ScriptedLLM('{"memories": []}')
    manager = _manager(tmp_path, llm)
    deferred = manager.finalize_turn(session, "先记下", extract_semantic=True)
    assert deferred["pending"] is True
    assert llm.calls == []
    assert manager.meter.extractions[-1].reason_codes == ("deferred_background",)
    session.begin_user_turn("新回合独有标记NEWTURNMARKER")
    session.append_message({"role": "user", "content": "新回合独有标记NEWTURNMARKER"})
    session.control_plane.finish_task(task.id, status="completed", steps_used=1, result="done")
    saved = manager.settle_previous_turn(session, root_turn_id=old_turn, task={"id": task.id})
    assert saved["episode_id"]
    assert len(llm.calls) == 1
    packet = llm.calls[0][-1]["content"]
    assert "记住我以后只用 bun" in packet
    assert "NEWTURNMARKER" not in packet
    assert "NEWTURNMARKER" in session.conversation_messages()[-1]["content"]


def test_provenance_rejects_invented_and_assistant_only_sources(tmp_path: Path):
    directory = tmp_path / "memory"
    service = MemoryService(
        semantic_store=SemanticMemoryStore(directory),
        episode_store=EpisodeStore(directory),
    )
    user = {"id": "ev-u-1", "kind": "user_statement", "summary": "记住我以后只用 bun"}
    assistant = {"id": "ev-a-1", "kind": "assistant_statement", "summary": "测试已经通过"}
    snapshot = _snapshot(user, assistant)

    def run(content: str):
        return extract_from_snapshot(
            snapshot,
            query=lambda _system, _user: _query_result(content),
            directory=directory,
            service=service,
        )

    skipped = extract_from_snapshot(
        _snapshot({"id": "ev-u-2", "kind": "user_statement", "summary": "当前目录"}),
        query=lambda *_args: (_ for _ in ()).throw(AssertionError("gate skipped")),
        directory=directory,
        service=service,
    )
    assert skipped.status == "skipped"
    assert skipped.attempted is False

    invented = run(json.dumps({"memories": [{
        "name": "bun",
        "type": "user",
        "content": "只用 bun",
        "action": "create",
        "source_refs": ["ev-made-up"],
    }]}))
    assert invented.status == "invalid"
    assert "unknown_source_ref" in invented.reason_codes
    assert list(directory.glob("*.md")) == []

    assistant_only = run(json.dumps({"memories": [{
        "name": "tests",
        "type": "project",
        "content": "测试已经通过",
        "action": "create",
        "source_refs": ["ev-a-1"],
    }]}))
    assert assistant_only.status == "invalid"
    assert "assistant_only" in assistant_only.reason_codes

    saved = run(json.dumps({"memories": [{
        "name": "package manager",
        "type": "user",
        "content": "以后只用 bun",
        "action": "create",
        "source_refs": ["ev-u-1"],
    }]}))
    assert saved.status == "succeeded"
    assert saved.written == 1
    record = service.semantic_store.list(read_scope="current_project", project_id="proj-test")[0]
    assert record.origin == "user_statement"
    assert record.scope == "project"
    assert record.project_id == "proj-test"
    assert record.locators[0].local_id == "ev-u-1"
    assert record.locators[0].session_id == "sess-test"
    assert record.locators[0].message_id == "msg_1"

    empty = run('{"memories": []}')
    assert empty.status == "empty"
    assert empty.attempted is True

    updated = run(json.dumps({"memories": [{
        "memory_id": record.id,
        "name": "package manager",
        "type": "user",
        "content": "以后只用 bun，不用 npm",
        "action": "update",
        "source_refs": ["ev-u-1"],
    }]}))
    assert updated.written == 1
    current = get_memory(record.id, directory)
    assert "不用 npm" in current.content
    assert current.locators[0].local_id == "ev-u-1"
    assert current.locators[0].session_id == "sess-test"
    cleared = update_memory(
        record.id,
        content="正文已改",
        directory=directory,
        expected_revision=current.revision,
        read_scope="current_project",
        project_id="proj-test",
    )
    assert cleared.origin == ""
    assert cleared.source_refs == ()


def test_old_semantic_file_is_not_loaded(tmp_path: Path):
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "old-note.md").write_text(
        "---\nname: old-note\ndescription: legacy\ntype: project\n"
        "created_at: 2024-01-01T00:00:00+00:00\nupdated_at: 2024-01-01T00:00:00+00:00\n"
        "---\n\nlegacy body\n",
        encoding="utf-8",
    )
    with pytest.raises(SemanticMemoryStoreError, match="不是当前语义记忆格式"):
        get_memory("old-note", directory)
    assert list(directory.glob("*.md")) == [directory / "old-note.md"]


def test_candidate_summary_keeps_tail_and_omits_usage():
    outcome = ("背景。" * 300) + "TAILTOKEN_BUN"
    episode = EpisodeRecord(
        id="ep-tail",
        session_id="s",
        goal="选择包管理器",
        status="failed",
        outcome=outcome,
        started_step=0,
        ended_step=1,
        created_at="2026-01-01T00:00:00Z",
        plan={},
        tools=({"name": "execute_command", "error": "ECONNREFUSED postgres", "ok": False, "status": "failed"},),
        agents=(),
        verification=(),
        usage={"prompt_tokens": 9000001, "completion_tokens": 0, "total_tokens": 9000001},
        version=3,
        project_id="alpha",
        project_root="/projects/alpha",
    )
    line = candidate_summary(episode, char_budget=700)
    assert "TAILTOKEN_BUN" in line
    assert "…(首尾截断)" in line
    assert "ECONNREFUSED" in line
    assert "9000001" not in line
    empty = EpisodeRecord(
        id="ep-empty-fail",
        session_id="s",
        goal="连接数据库",
        status="failed",
        outcome="",
        started_step=0,
        ended_step=1,
        created_at="2026-01-01T00:00:00Z",
        plan={},
        tools=({"name": "execute_command", "error": "ECONNREFUSED postgres", "ok": False, "status": "failed"},),
        agents=(),
        verification=(),
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        project_id="alpha",
    )
    assert "ECONNREFUSED" in candidate_summary(empty)


def test_selector_failure_and_zero_budget_do_not_inject(tmp_path: Path):
    store = EpisodeStore(tmp_path / "memory")
    episode = store.save(EpisodeRecord(
        id="ep-login",
        session_id="s",
        goal="修复登录",
        status="completed",
        outcome="改了 cookie",
        started_step=0,
        ended_step=1,
        created_at="2026-01-01T00:00:00Z",
        plan={},
        tools=(),
        agents=(),
        verification=(),
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        project_id="alpha",
        project_root="/alpha",
    ))

    class _Bad:
        def select(self, **kwargs):
            return SelectorChoice(
                failed=True,
                episodes_usable=False,
                failure_type="invalid_json",
                episode_ids=(episode.id,),
            )

    service = MemoryService(
        semantic_store=SemanticMemoryStore(tmp_path / "memory"),
        episode_store=store,
        selector=_Bad(),
    )
    context = service.prepare_memory_context("登录", project_id="alpha")
    assert context.episode_text == ""
    assert context.selected_episode_ids == ()
    assert episode.goal not in context.prompt_injection

    class _Pick:
        def select(self, **kwargs):
            return SelectorChoice(episode_ids=(episode.id,))

    tight = MemoryService(
        semantic_store=SemanticMemoryStore(tmp_path / "memory"),
        episode_store=store,
        episode_policy=EpisodePolicy(max_episode_tokens_budget=0),
        selector=_Pick(),
    )
    blocked = tight.prepare_memory_context("登录", project_id="alpha")
    assert blocked.selected_episode_ids == (episode.id,)
    assert blocked.rendered_episode_ids == ()
    assert blocked.episode_text == ""
    assert episode.id in blocked.budget_skipped_episode_ids


def test_one_selector_per_user_turn(tmp_path: Path):
    session = _session(tmp_path, "修复登录")
    pid = project_id(session.project_root)
    manager = _manager(tmp_path)
    manager.episode_store.save(EpisodeRecord(
        id="ep-turn",
        session_id=session.session_id,
        goal="修复登录",
        status="completed",
        outcome="cookie",
        started_step=0,
        ended_step=1,
        created_at="2026-01-01T00:00:00Z",
        plan={},
        tools=(),
        agents=(),
        verification=(),
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        project_id=pid,
        project_root=str(session.project_root),
    ))

    class _Count:
        def __init__(self):
            self.calls = 0

        def select(self, **kwargs):
            self.calls += 1
            return SelectorChoice(episode_ids=())

    counter = _Count()
    manager.bind_project(session.project_root)
    manager.service.selector = counter
    manager.recall_for_turn(session)
    manager.recall_for_turn(session)
    assert counter.calls == 1
    session.begin_user_turn("下一个任务")
    session.append_message({"role": "user", "content": "下一个任务"})
    manager.recall_for_turn(session)
    assert counter.calls == 2


def test_meter_separates_calls_and_does_not_invent_cost():
    meter = MemoryMeter()
    meter.record_model_call(
        "extraction",
        status="failed",
        attempts=1,
        usage=UsageRecord(5, 1, 6),
        model="scripted-memory",
    )
    meter.record_model_call("selection", status="skipped", reason_codes=("no_selector_call",), attempts=0)
    meter.note_injection(
        selected_episode_ids=("ep-a", "ep-b", "ep-c"),
        rendered_episode_ids=("ep-a", "ep-b"),
        budget_skipped_ids=("ep-c",),
        semantic_estimated_tokens=10,
        episode_estimated_tokens=80,
        omitted=True,
    )
    payload = meter.to_dict()
    assert payload["extractions"][0]["total_tokens"] == 6
    assert payload["extractions"][0]["kind"] == "extraction"
    assert payload["selections"][0]["usage_available"] is False
    assert payload["selections"][0]["prompt_tokens"] is None
    injection = payload["injections"][0]
    assert injection["injected_episode_ids"] == []
    assert injection["episode_estimated_tokens"] == 0
    assert injection["selected_episode_ids"] == ["ep-a", "ep-b", "ep-c"]
    assert injection["rendered_episode_ids"] == ["ep-a", "ep-b"]
    assert "total_tokens" not in injection
    meter.record_model_call("extraction", status="ok", usage="not-a-record")  # type: ignore[arg-type]
    assert len(meter.extractions) == 1


def test_observer_error_does_not_drop_usage_or_the_turn(tmp_path: Path):
    session = _session(tmp_path, "记住我以后只用 bun")
    session.mark_completed()
    llm = _ScriptedLLM('{"memories": []}', fail_after_usage=True)
    manager = _manager(tmp_path, llm)
    seen: list[UsageRecord] = []

    def observe(usage: UsageRecord) -> None:
        seen.append(usage)
        raise RuntimeError("observer down")

    manager.usage_observer = observe
    outcome = manager.finalize_turn(session, "好", extract_semantic=True)
    assert outcome["episode_id"]
    assert seen and seen[-1].total_tokens == 6
    assert manager.meter.extractions[-1].status == "failed"
    assert manager.meter.extractions[-1].usage_available is True
    assert manager.meter.extractions[-1].total_tokens == 6


def test_english_durable_phrase_and_ordinary_edit_gate():
    policy = SemanticExtractPolicy()
    english = policy.decide((ExtractSignal("ev-u-1", "user_statement", text="from now on use bun"),))
    assert english.should_extract is True
    edit = policy.decide((
        ExtractSignal("ev-u-1", "user_statement", text="改一下 README 的标题"),
        ExtractSignal("ev-t-1", "tool_observation", subject="README.md", ok=True, execution_status="succeeded"),
    ))
    assert edit.should_extract is False
    assert edit.reason_codes == ("no_durable_signal",)
    weather = policy.decide((
        ExtractSignal("ev-u-1", "user_statement", text="查一下天气"),
        ExtractSignal("ev-t-1", "tool_observation", command="curl wttr.in", ok=True),
    ))
    assert weather.should_extract is False


def test_projects_do_not_share_automatic_candidates(tmp_path: Path):
    store = EpisodeStore(tmp_path)
    store.save(EpisodeRecord(
        id="ep-alpha",
        session_id="s",
        goal="修复登录接口返回 401",
        status="completed",
        outcome="cookie",
        started_step=0,
        ended_step=1,
        created_at="2026-01-01T00:00:00Z",
        plan={},
        tools=(),
        agents=(),
        verification=(),
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        project_id="alpha",
        project_root="/alpha",
    ))
    store.save(EpisodeRecord(
        id="ep-beta",
        session_id="s",
        goal="修复登录接口返回 401",
        status="completed",
        outcome="cookie",
        started_step=0,
        ended_step=1,
        created_at="2026-01-02T00:00:00Z",
        plan={},
        tools=(),
        agents=(),
        verification=(),
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        project_id="beta",
        project_root="/beta",
    ))
    hits = store.search("登录 401", scope="current_project", project_id="alpha")
    assert [hit.episode.id for hit in hits] == ["ep-alpha"]
