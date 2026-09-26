"""Unit tests for the refined DDD memory architecture:

1. FactPolicy: secret guardrails, precedence, deduplication.
2. EpisodePolicy: worthiness threshold, similarity cutoff, token budgeting, decay weighting.
3. MemoryService: dual-track retrieval, DTO projection, and session consolidation.
"""

from __future__ import annotations

import time
import pytest

from wright.domain.model.memory import Episode, Fact
from wright.domain.policy.memory import EpisodePolicy, FactPolicy, is_safe_fact
from wright.domain.gateway.memory import IEpisodicMemoryStore, IFactRepository
from wright.application.memory.memory_service import MemoryService
from wright.application.memory.dto import MemoryContextDTO


def test_fact_policy_guardrails():
    # 1. Secret patterns must be blocked
    safe, reason = is_safe_fact("User prefers uv over poetry")
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
        safe, reason = is_safe_fact(key)
        assert safe is False
        assert "Sensitive credential pattern detected" in reason

    policy = FactPolicy()
    f1 = Fact(id="f1", content="Normal content")
    assert policy.validate_fact(f1)[0] is True

    f2 = Fact(id="f2", content="sk-abcdef1234567890abcdef1234567890")
    assert policy.validate_fact(f2)[0] is False


def test_fact_policy_precedence_and_deduplication():
    policy = FactPolicy(project_overrides_global=True)

    global_fact = Fact(
        id="f1",
        key="package_manager",
        content="pnpm",
        scope="GLOBAL",
    )
    project_fact = Fact(
        id="f2",
        key="package_manager",
        content="uv",
        scope="PROJECT",
    )

    # Project overrides global regardless of order
    assert policy.resolve_precedence(global_fact, project_fact).content == "uv"
    assert policy.resolve_precedence(project_fact, global_fact).content == "uv"

    # Same scope: incoming overrides existing
    updated_project = Fact(
        id="f3",
        key="package_manager",
        content="poetry",
        scope="PROJECT",
    )
    assert policy.resolve_precedence(project_fact, updated_project).content == "poetry"

    # Deduplication
    facts = [global_fact, project_fact, Fact(id="f4", key="db", content="sqlite")]
    deduped = policy.deduplicate_facts(facts)
    assert len(deduped) == 2
    by_key = {f.key: f.content for f in deduped}
    assert by_key["package_manager"] == "uv"
    assert by_key["db"] == "sqlite"


def test_episode_policy_worthiness_and_budgeting():
    policy = EpisodePolicy(min_similarity_threshold=0.75, max_episode_tokens_budget=300)

    # Worthiness
    assert policy.is_worthy_of_recording(task_steps=1, has_error_resolved=False, outcome="FAILED") is False
    assert policy.is_worthy_of_recording(task_steps=1, has_error_resolved=False, outcome="SUCCESS") is False
    assert policy.is_worthy_of_recording(task_steps=1, has_error_resolved=True, outcome="SUCCESS") is True
    assert policy.is_worthy_of_recording(task_steps=4, has_error_resolved=False, outcome="SUCCESS") is True

    # Budgeting & filtering
    e1 = Episode(id="e1", task_description="Task 1", resolution="Res 1", token_count=100)
    e2 = Episode(id="e2", task_description="Task 2", resolution="Res 2", token_count=150)
    e3 = Episode(id="e3", task_description="Task 3", resolution="Res 3", token_count=100)
    e4 = Episode(id="e4", task_description="Task 4", resolution="Res 4", token_count=50)

    candidates = [
        (e1, 0.90),  # In (100 tokens, used 100)
        (e2, 0.85),  # In (150 tokens, used 250)
        (e3, 0.80),  # Budget overflow (250+100 > 300) -> stopped
        (e4, 0.60),  # Score < 0.75
    ]
    budgeted = policy.budget_and_filter(candidates)
    assert len(budgeted) == 2
    assert budgeted[0].id == "e1"
    assert budgeted[1].id == "e2"

    # Decay calculation
    now = time.time()
    weight_fresh = policy.calculate_decay_weight(now, now)
    assert weight_fresh == 1.0

    weight_30d = policy.calculate_decay_weight(now - 30 * 86400, now, half_life_days=30.0)
    assert pytest.approx(weight_30d, 0.01) == 0.5


class MockFactRepository(IFactRepository):
    def __init__(self, initial_facts: list[Fact] | None = None) -> None:
        self.facts = initial_facts or []

    def get_facts(self, scope: str | None = None) -> list[Fact]:
        if scope:
            return [f for f in self.facts if f.scope == scope]
        return list(self.facts)

    def save_fact(self, fact: Fact) -> None:
        self.facts = [f for f in self.facts if (f.key or f.id) != (fact.key or fact.id)]
        self.facts.append(fact)

    def delete_fact(self, fact_id: str) -> bool:
        before = len(self.facts)
        self.facts = [f for f in self.facts if f.id != fact_id]
        return len(self.facts) < before


class MockEpisodicStore(IEpisodicMemoryStore):
    def __init__(self, episodes: list[tuple[Episode, float]] | None = None) -> None:
        self.episodes = episodes or []
        self.saved: list[Episode] = []

    def search_episodes(self, query: str, top_k: int = 3) -> list[tuple[Episode, float]]:
        return self.episodes[:top_k]

    def record_episode(self, episode: Episode) -> None:
        self.saved.append(episode)


def test_memory_service_orchestration():
    f1 = Fact(id="f1", key="tool", content="Use pytest", scope="PROJECT")
    f2 = Fact(id="f2", key="secret", content="sk-1234567890abcdef1234567890", scope="PROJECT")
    e1 = Episode(id="e1", task_description="Fix db migration", resolution="Rolled back and added check", token_count=50)

    fact_repo = MockFactRepository([f1, f2])
    episode_store = MockEpisodicStore([(e1, 0.95)])

    service = MemoryService(
        fact_repo=fact_repo,
        episode_store=episode_store,
    )

    # 1. Dual-track context preparation
    ctx = service.prepare_memory_context("run unit tests")
    assert isinstance(ctx, MemoryContextDTO)
    # The unsafe secret fact must be filtered out
    assert len(ctx.facts) == 1
    assert ctx.facts[0].content == "Use pytest"
    # Episode with high score is included
    assert len(ctx.episodes) == 1
    assert ctx.episodes[0].id == "e1"
    # Prompt injection markdown rendered
    assert "### Project Facts & User Preferences" in ctx.prompt_injection
    assert "### Relevant Historical Troubleshooting & Lessons" in ctx.prompt_injection

    # 2. Record new fact with validation
    ok, err = service.record_fact(content="sk-abcdef1234567890abcdef", key="token")
    assert ok is False
    assert "Sensitive credential pattern detected" in err

    ok, err = service.record_fact(content="Always use uv sync", key="sync")
    assert ok is True
    assert err is None
    assert any(f.key == "sync" for f in fact_repo.facts)

    # 3. Consolidate session
    # Trivial -> not recorded
    worthy = service.consolidate_session(
        session_id="s1",
        task_description="ls",
        outcome="SUCCESS",
        task_steps=1,
        has_error_resolved=False,
    )
    assert worthy is False
    assert len(episode_store.saved) == 0

    # Complex / error resolved -> recorded
    worthy = service.consolidate_session(
        session_id="s2",
        task_description="Fixed memory leak in server",
        outcome="SUCCESS",
        task_steps=5,
        has_error_resolved=True,
        resolution="Closed socket on disconnect",
    )
    assert worthy is True
    assert len(episode_store.saved) == 1
    assert episode_store.saved[0].resolution == "Closed socket on disconnect"
