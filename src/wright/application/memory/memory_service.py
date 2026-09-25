"""MemoryService: Unified application facade for core, factual, and episodic memory orchestration."""

from __future__ import annotations

from typing import Any, Sequence

from ...domain.gateway.memory import ICoreMemoryStore, IEpisodicMemoryStore, IFactRepository
from ...domain.model.memory import CoreMemory, Episode, Fact, FactScope
from ...domain.policy.memory import CoreMemoryPolicy, EpisodePolicy, FactPolicy
from .dto import MemoryContextDTO


class MemoryService:
    """Facade application service orchestrating the complete 4-layer memory pyramid."""

    def __init__(
        self,
        fact_repo: IFactRepository,
        episode_store: IEpisodicMemoryStore,
        core_memory_store: ICoreMemoryStore | None = None,
        fact_policy: FactPolicy | None = None,
        episode_policy: EpisodePolicy | None = None,
        core_memory_policy: CoreMemoryPolicy | None = None,
    ) -> None:
        self.fact_repo = fact_repo
        self.episode_store = episode_store
        self.core_memory_store = core_memory_store
        self.fact_policy = fact_policy or FactPolicy()
        self.episode_policy = episode_policy or EpisodePolicy()
        self.core_memory_policy = core_memory_policy or CoreMemoryPolicy()

    def prepare_memory_context(
        self,
        current_task: str,
        *,
        scope: FactScope = "PROJECT",
        max_facts: int = 10,
    ) -> MemoryContextDTO:
        """Pyramid memory recall:
        1. Core Memory: Pinned high-priority instructions, persona, and constraints (100% pinned).
        2. Factual Memory: Load deterministic facts/preferences (cheap, high-precision).
        3. Episodic Memory: Search similar cases/troubleshooting experiences (budgeted).
        """
        blocks: list[str] = []

        # 1. Core Memory (Always pinned at the very top)
        core_mem: CoreMemory | None = None
        if self.core_memory_store is not None:
            core_mem = self.core_memory_store.load()
            if core_mem is not None:
                blocks.append(core_mem.render_block())

        # 2. Facts track
        raw_facts = self.fact_repo.get_facts(scope=scope)
        safe_facts = self.fact_policy.filter_safe(raw_facts)
        deduped_facts = self.fact_policy.deduplicate_facts(safe_facts)[:max_facts]

        if deduped_facts:
            facts_lines = [
                f"- [{f.scope}] {f.key}: {f.content}" if f.key else f"- [{f.scope}] {f.content}"
                for f in deduped_facts
            ]
            blocks.append("### Project Facts & User Preferences\n" + "\n".join(facts_lines))

        # 3. Episode track
        scored_episodes = self.episode_store.search_episodes(current_task, top_k=5)
        budgeted_episodes = self.episode_policy.budget_and_filter(scored_episodes)

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
            core_memory=core_mem,
            facts=tuple(deduped_facts),
            episodes=tuple(budgeted_episodes),
            prompt_injection=prompt_injection,
        )

    def update_core_memory(
        self,
        section: str,
        content: str,
        mode: str = "append",
    ) -> tuple[bool, str | None]:
        """Validate and update the agent's pinned core memory."""
        if self.core_memory_store is None:
            return False, "Core memory store is not configured"

        is_valid, err = self.core_memory_policy.validate_update(section, content)
        if not is_valid:
            return False, err

        core_mem = self.core_memory_store.load()
        normalized_section = section.strip().lower()
        new_text = content.strip()

        if normalized_section == "human_profile":
            final_text = (
                f"{core_mem.human_profile}\n- {new_text}"
                if mode == "append" and core_mem.human_profile
                else new_text
            )
            is_len_valid, len_err = self.core_memory_policy.validate_update(
                normalized_section, final_text
            )
            if not is_len_valid:
                return False, len_err
            core_mem.update_human_profile(final_text)
        elif normalized_section == "project_anchor":
            final_text = (
                f"{core_mem.project_anchor}\n- {new_text}"
                if mode == "append" and core_mem.project_anchor
                else new_text
            )
            is_len_valid, len_err = self.core_memory_policy.validate_update(
                normalized_section, final_text
            )
            if not is_len_valid:
                return False, len_err
            core_mem.update_project_anchor(final_text)
        else:
            return False, f"Unsupported section: {section}"

        self.core_memory_store.save(core_mem)
        return True, None

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
