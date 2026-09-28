"""One-turn recall selection. It uses the other memory operations; it does not write stores."""

from __future__ import annotations

from dataclasses import replace

from ...core.logger import get_logger
from ...domain.model.memory import (
    EpisodeRecord,
    SemanticMemoryRecord,
)
from ...domain.policy.memory import (
    EpisodePolicy,
)
from .dto import MemoryContextDTO
from .projection import (
    budget_episode_manifest,
    estimate_recall_text,
)

logger = get_logger(__name__)

class RecallCoordinator:
    def __init__(self, core, semantic, episodes, selector, episode_policy: EpisodePolicy, generation) -> None:
        self.core = core
        self.semantic = semantic
        self.episodes = episodes
        self.selector = selector
        self.episode_policy = episode_policy
        self.generation = generation

    def prepare_memory_context(
        self,
        current_task: str,
        *,
        project_id: str = "",
        max_memories: int = 200,
    ) -> MemoryContextDTO:
        """Select semantic and episode recall for one task.

        Core memory is returned for callers and is injected by the system-prompt
        projection, not by this recall block. Semantic candidates are filtered
        by scope and status before the selector budget. A selector failure
        leaves episodes empty and still returns only that scoped index.
        """
        del max_memories  # The semantic policy owns the candidate cap.
        core_mem = self.core._load_core(project_id)
        memories = self.semantic._semantic_candidates(project_id)
        selectable, semantic_manifest = self.semantic._budget_semantic_manifest(memories)
        candidates = (
            self.episodes._episode_candidates(current_task, project_id) if project_id else []
        )
        selected_memories: list[SemanticMemoryRecord] = []
        selected_episodes: list[EpisodeRecord] = []
        selector_attempted = False
        selector_failed = False
        selector_failure_type = ""
        if self.selector is not None and (selectable or candidates or semantic_manifest):
            selector_attempted = True
            episode_manifest, _dropped = budget_episode_manifest(
                [hit.episode for hit in candidates],
                token_budget=self.episode_policy.selector_input_token_budget,
            )
            choice = self.selector.select(
                task=current_task,
                semantic_manifest=semantic_manifest,
                episode_manifest=episode_manifest,
            )
            selector_failed = bool(choice.failed)
            selector_failure_type = choice.failure_type
            if choice.failed:
                logger.info("episode_selector failure_type=%s", choice.failure_type or "invalid")
            else:
                selected_memories = self.semantic._take_memories(list(selectable), choice.memory_ids)
                if choice.episodes_usable:
                    selected_episodes = self.episodes._take_episodes(candidates, choice.episode_ids)
                else:
                    logger.info(
                        "episode_selector failure_type=%s",
                        choice.failure_type or "invalid_episode_field",
                    )
        elif candidates or memories:
            logger.info("episode_selector failure_type=%s", "unavailable")

        semantic_text = self.semantic._render_semantic(memories, selected_memories, project_id)
        episode_text, episode_cost, skipped, rendered_ids = self.episodes._render_episodes(
            selected_episodes
        )
        logger.info(
            "episode_recall candidates=%d selected=%s budget_skipped=%s",
            len(candidates),
            [episode.id for episode in selected_episodes],
            list(skipped),
        )
        parts = [part for part in (semantic_text, episode_text) if part]
        return MemoryContextDTO(
            core_memory=core_mem,
            memories=tuple(selected_memories),
            episodes=tuple(selected_episodes),
            semantic_text=semantic_text,
            episode_text=episode_text,
            selected_memory_ids=tuple(record.id for record in selected_memories),
            selected_episode_ids=tuple(episode.id for episode in selected_episodes),
            episode_estimated_tokens=episode_cost,
            semantic_estimated_tokens=estimate_recall_text(semantic_text) if semantic_text else 0,
            rendered_episode_ids=rendered_ids,
            budget_skipped_episode_ids=skipped,
            prompt_injection="\n\n".join(parts),
            project_id=project_id,
            selector_attempted=selector_attempted,
            selector_failed=selector_failed,
            selector_failure_type=selector_failure_type,
            semantic_generation=self.generation(),
        )

    def reproject_semantic(self, previous: MemoryContextDTO) -> MemoryContextDTO:
        """Refresh semantic text for the same selection. Does not call the selector."""
        project_id = previous.project_id
        memories = self.semantic._semantic_candidates(project_id)
        selected = self.semantic._take_memories(memories, previous.selected_memory_ids)
        semantic_text = self.semantic._render_semantic(memories, selected, project_id)
        parts = [part for part in (semantic_text, previous.episode_text) if part]
        return replace(
            previous,
            memories=tuple(selected),
            semantic_text=semantic_text,
            selected_memory_ids=tuple(record.id for record in selected),
            semantic_estimated_tokens=estimate_recall_text(semantic_text) if semantic_text else 0,
            prompt_injection="\n\n".join(parts),
            semantic_generation=self.generation(),
        )
