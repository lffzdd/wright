"""Unit tests for the refined DDD memory architecture:

1. SemanticMemoryPolicy: secret guardrails.
2. EpisodePolicy: admission and token-budget selection.
3. MemoryService: core, semantic, and episode recall.
"""

from __future__ import annotations

from pathlib import Path

from wright.application.memory.dto import MemoryContextDTO
from wright.application.memory.memory_service import MemoryService
from wright.domain.gateway.memory import (
    IEpisodeStore,
    ISemanticMemoryStore,
    SelectorChoice,
)
from wright.domain.model.memory import (
    EpisodeRecord,
    EpisodeSearchHit,
    SemanticMemoryRecord,
)
from wright.domain.policy.memory import (
    EpisodePolicy,
    ExtractSignal,
    SemanticExtractPolicy,
    SemanticMemoryPolicy,
    is_safe_memory,
)


def test_semantic_memory_policy_guardrails():
    safe, reason = is_safe_memory("User prefers uv over poetry")
    assert safe is True
    assert reason is None

    unsafe_keys = [
        "My key is sk-abcdef1234567890abcdef1234567890",
        "ghp_123456789012345678901234567890123456",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...",
        "postgres://user:super_secret_pw@localhost:5432/db",
        'api_key = "1234567890abcdef12345678"',
    ]
    for key in unsafe_keys:
        safe, reason = is_safe_memory(key)
        assert safe is False
        assert "Sensitive credential pattern detected" in reason

    policy = SemanticMemoryPolicy()
    assert policy.validate("Normal content")[0] is True
    assert policy.validate("sk-abcdef1234567890abcdef1234567890")[0] is False
    assert policy.validate("   ")[0] is False


def test_episode_policy_admission_and_budget():
    policy = EpisodePolicy()

    blank = policy.admission(
        tool_count=0, agent_count=0, user_texts=["  "], has_delivered_answer=False
    )
    greeting = policy.admission(
        tool_count=0, agent_count=0, user_texts=["谢谢！"], has_delivered_answer=True
    )
    answer = policy.admission(
        tool_count=0, agent_count=0, user_texts=["修复登录"], has_delivered_answer=True
    )
    no_answer = policy.admission(
        tool_count=0, agent_count=0, user_texts=["修复登录"], has_delivered_answer=False
    )
    assert blank.keep is False and blank.skip_reason == "blank"
    assert greeting.keep is False and greeting.skip_reason == "greeting"
    assert no_answer.keep is False and no_answer.skip_reason == "no_answer"
    assert answer.keep is True
    assert policy.admission(
        tool_count=1, agent_count=0, user_texts=["hi"], has_delivered_answer=False
    ).keep is True
    assert policy.admission(
        tool_count=0, agent_count=1, user_texts=[], has_delivered_answer=False
    ).keep is True

    assert policy.accepts_cost(100, 100) is True
    assert policy.accepts_cost(401, 800) is False
    assert policy.accepts_cost(10, 0) is False
    assert policy.item_token_limit(0) == 0
    assert EpisodePolicy(max_episode_tokens_budget=0).accepts_cost(1, 100) is False
    assert policy.lexical_candidate_limit == 40
    assert policy.recent_candidate_limit == 10
    assert policy.max_selected_episodes == 3
    assert policy.max_episode_tokens_budget == 800
    assert policy.max_single_episode_tokens == 400


def _memory(memory_id: str, name: str, content: str, type_: str = "project") -> SemanticMemoryRecord:
    return SemanticMemoryRecord(
        id=memory_id,
        name=name,
        description="",
        type=type_,  # type: ignore[arg-type]
        content=content,
        created_at="",
        updated_at="",
        path=Path(f"{memory_id}.md"),
    )


class MockSemanticStore(ISemanticMemoryStore):
    def __init__(self, memories: list[SemanticMemoryRecord] | None = None) -> None:
        self.memories = list(memories or [])

    def list(self, limit: int = 100) -> list[SemanticMemoryRecord]:
        return self.memories[:limit]

    def get(self, memory_id: str) -> SemanticMemoryRecord:
        raise AssertionError(memory_id)

    def search(self, query: str = "", *, limit: int = 20) -> list[SemanticMemoryRecord]:
        return self.memories[:limit]

    def save(self, *, name: str, description: str, type_: str, content: str) -> SemanticMemoryRecord:
        record = _memory(name, name, content, type_)
        self.memories.append(record)
        return record

    def delete(self, memory_id: str) -> SemanticMemoryRecord:
        raise AssertionError(memory_id)


def _record(episode_id: str, goal: str, outcome: str, tokens: int) -> EpisodeRecord:
    return EpisodeRecord(
        id=episode_id,
        session_id="s",
        goal=goal,
        status="completed",
        outcome=outcome,
        started_step=0,
        ended_step=1,
        created_at="2026-09-26T00:00:00Z",
        plan={},
        tools=(),
        agents=(),
        verification=(),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": tokens},
    )


class MockEpisodeStore(IEpisodeStore):
    def __init__(self, episodes: list[tuple[EpisodeRecord, float]] | None = None) -> None:
        self.episodes = episodes or []

    def save(self, episode: EpisodeRecord) -> EpisodeRecord:
        return episode

    def get(self, episode_id: str) -> EpisodeRecord:
        raise AssertionError(episode_id)

    def delete(self, episode_id: str) -> EpisodeRecord:
        raise AssertionError(episode_id)

    def list(self, limit: int = 100, *, project_id=None, scope=None) -> list[EpisodeRecord]:
        del project_id, scope
        return [episode for episode, _score in self.episodes[:limit]]

    def search(self, query: str = "", *, status=None, limit: int = 20, scope="current_project", project_id=""):
        del query, status, scope, project_id
        return [
            EpisodeSearchHit(episode=episode, lexical_score=score)
            for episode, score in self.episodes[:limit]
        ]

    def recent(self, *, project_id: str, limit: int = 10) -> list[EpisodeRecord]:
        del project_id
        return [episode for episode, _score in self.episodes[:limit]]


def test_memory_service_orchestration():
    safe_memory = _memory("tool", "tool", "Use pytest")
    secret_memory = _memory(
        "secret", "secret", "sk-1234567890abcdef1234567890"
    )
    e1 = _record("ep-e1", "Fix db migration", "Rolled back and added check", 50)

    class Picker:
        def select(self, *, task, semantic_manifest, episode_manifest):
            del task, semantic_manifest, episode_manifest
            return SelectorChoice(memory_ids=("tool.md",), episode_ids=("ep-e1",))

    semantic_store = MockSemanticStore([safe_memory, secret_memory])
    episode_store = MockEpisodeStore([(e1, 4.2)])

    service = MemoryService(
        semantic_store=semantic_store,
        episode_store=episode_store,
        selector=Picker(),
    )

    ctx = service.prepare_memory_context("run unit tests", project_id="wright-proj")
    assert isinstance(ctx, MemoryContextDTO)
    assert ctx.project_id == "wright-proj"
    assert len(ctx.memories) == 1
    assert ctx.memories[0].content == "Use pytest"
    assert len(ctx.episodes) == 1
    assert ctx.episodes[0].id == "ep-e1"
    assert "wright-semantic-recall" in ctx.semantic_text
    assert "ep-e1" in ctx.episode_text
    assert ctx.episode_estimated_tokens > 0
    assert ctx.episode_estimated_tokens <= 800

    saved, err = service.record_memory(name="token", content="sk-abcdef1234567890abcdef")
    assert saved is None
    assert err is not None
    assert "Sensitive credential pattern detected" in err

    saved, err = service.record_memory(name="sync", content="Always use uv sync")
    assert err is None
    assert saved is not None
    assert saved.name == "sync"
    assert any(record.name == "sync" for record in semantic_store.memories)


def test_semantic_extract_skips_greetings_and_keeps_real_turns():
    policy = SemanticExtractPolicy()

    def decision(*signals: ExtractSignal):
        return policy.decide(signals)

    assert decision(ExtractSignal("ev-u-1", "user_statement", text="你好")).should_extract is False
    assert decision(ExtractSignal("ev-u-1", "user_statement", text="谢谢！")).should_extract is False
    assert decision(
        ExtractSignal("ev-u-1", "user_statement", text="hi"),
        ExtractSignal("ev-u-2", "user_statement", text="hello"),
    ).should_extract is False
    assert decision().should_extract is False
    durable = decision(ExtractSignal("ev-u-1", "user_statement", text="记住我用 bun"))
    assert durable.should_extract is True
    assert durable.reason_codes == ("user_durable_statement",)
    assert decision(
        ExtractSignal("ev-u-1", "user_statement", text="hi"),
        ExtractSignal("ev-t-1", "tool_observation", command="pwd", ok=True, execution_status="succeeded"),
    ).should_extract is False


def test_selector_failure_does_not_inject_keyword_episodes():
    e1 = _record("ep-e1", "Fix db migration", "Rolled back and added check", 50)

    class Broken:
        def select(self, *, task, semantic_manifest, episode_manifest):
            del task, semantic_manifest, episode_manifest
            return SelectorChoice(failed=True, episodes_usable=False, failure_type="RuntimeError")

    service = MemoryService(
        semantic_store=MockSemanticStore(),
        episode_store=MockEpisodeStore([(e1, 9.0)]),
        selector=Broken(),
    )
    ctx = service.prepare_memory_context("migration", project_id="wright-proj")
    assert ctx.episodes == ()
    assert ctx.episode_text == ""
