"""MemoryService: Unified application facade for factual and episodic memory orchestration."""

from __future__ import annotations

from typing import Any, Sequence

from ...domain.gateway.episode_gateway import IEpisodicMemoryStore
from ...domain.gateway.fact_gateway import IFactRepository
from ...domain.model.episode import Episode
from ...domain.model.fact import Fact, FactScope
from ...domain.policy.episode_policy import EpisodePolicy
from ...domain.policy.fact_policy import FactPolicy
from .dto import MemoryContextDTO


class MemoryService:
    """Facade application service orchestrating long-term memory retrieval and consolidation."""

    def __init__(
        self,
        fact_repo: IFactRepository,
        episode_store: IEpisodicMemoryStore,
        fact_policy: FactPolicy | None = None,
        episode_policy: EpisodePolicy | None = None,
    ) -> None:
        self.fact_repo = fact_repo
        self.episode_store = episode_store
        self.fact_policy = fact_policy or FactPolicy()
        self.episode_policy = episode_policy or EpisodePolicy()

    def prepare_memory_context(
        self,
        current_task: str,
        *,
        scope: FactScope = "PROJECT",
        max_facts: int = 10,
    ) -> MemoryContextDTO:
        """Dual-track recall:
        1. Factual Memory: Load deterministic facts/preferences (cheap, high-precision).
        2. Episodic Memory: Search similar cases/troubleshooting experiences (budgeted).
        """
        # 1. Facts track
        raw_facts = self.fact_repo.get_facts(scope=scope)
        safe_facts = self.fact_policy.filter_safe(raw_facts)
        deduped_facts = self.fact_policy.deduplicate_facts(safe_facts)[:max_facts]

        # 2. Episode track
        scored_episodes = self.episode_store.search_episodes(current_task, top_k=5)
        budgeted_episodes = self.episode_policy.budget_and_filter(scored_episodes)

        # 3. Format prompt injection
        blocks: list[str] = []
        if deduped_facts:
            facts_lines = [
                f"- [{f.scope}] {f.key}: {f.content}" if f.key else f"- [{f.scope}] {f.content}"
                for f in deduped_facts
            ]
            blocks.append("### Project Facts & User Preferences\n" + "\n".join(facts_lines))

        if budgeted_episodes:
            episodes_lines = []
            for ep in budgeted_episodes:
                desc = getattr(ep, "task_description", "") or getattr(ep, "goal", "")
                res = getattr(ep, "resolution", "") or getattr(ep, "outcome", "")
                episodes_lines.append(f"- **Task:** {desc}\n  **Resolution:** {res}")
            blocks.append(
                "### Relevant Historical Troubleshooting & Lessons\n" + "\n".join(episodes_lines)
            )

        prompt_injection = "\n\n".join(blocks)

        return MemoryContextDTO(
            facts=tuple(deduped_facts),
            episodes=tuple(budgeted_episodes),
            prompt_injection=prompt_injection,
        )

    def record_fact(
        self,
        content: str,
        key: str = "",
        scope: FactScope = "PROJECT",
    ) -> tuple[bool, str | None]:
        """Validate against domain policy and persist a new or updated fact."""
        fact = Fact(
            id=key or f"fact-{abs(hash(content)) % 1000000}",
            key=key,
            content=content,
            scope=scope,
        )
        is_valid, reason = self.fact_policy.validate_fact(fact)
        if not is_valid:
            return False, reason
        self.fact_repo.save_fact(fact)
        return True, None

    def consolidate_session(
        self,
        session_id: str,
        task_description: str,
        outcome: str,
        task_steps: int = 1,
        has_error_resolved: bool = False,
        resolution: str = "",
    ) -> bool:
        """Consolidate an execution session into episodic memory if worthy."""
        if not self.episode_policy.is_worthy_of_recording(
            task_steps=task_steps,
            has_error_resolved=has_error_resolved,
            outcome=outcome,
        ):
            return False

        episode = Episode(
            id=f"ep-{session_id}-{abs(hash(task_description)) % 100000}",
            task_description=task_description,
            trigger_error=resolution if outcome != "SUCCESS" else None,
            resolution=resolution or outcome,
            outcome="SUCCESS" if outcome.upper() in {"SUCCESS", "COMPLETED"} else "FAILURE",
        )
        self.episode_store.record_episode(episode)
        return True


__all__ = ["MemoryService"]
