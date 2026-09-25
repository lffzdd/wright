"""Episodic memory policy (Pure business rules, zero I/O).

Responsible for:
1. Worthiness threshold: determining whether a session outcome is worth persisting as an episode.
2. Relevance and Token budget: pruning similar episodes to prevent context window bloat.
3. Time decay: calculating recency weighting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from ..model.episode import Episode


@dataclass(frozen=True)
class EpisodePolicy:
    """Pure domain rules governing episodic case study recording, filtering, and budgeting."""

    min_similarity_threshold: float = 0.75
    max_episode_tokens_budget: int = 800
    min_steps_for_worthy: int = 3
    default_half_life_days: float = 30.0

    def is_worthy_of_recording(
        self,
        task_steps: int,
        has_error_resolved: bool,
        outcome: str,
    ) -> bool:
        """Business rule: Determine whether an execution warrants persistence as an Episode.
        
        Only cases that:
        1. Successfully finished (outcome in {"SUCCESS", "completed"}); AND
        2. Either resolved an error (debugging experience) OR took >= min_steps (complex workflow)
        are preserved. Trivial single-step executions (e.g., simple ls/pwd) are ignored.
        """
        normalized_outcome = outcome.upper()
        if normalized_outcome not in {"SUCCESS", "COMPLETED"}:
            return False
        return has_error_resolved or task_steps >= self.min_steps_for_worthy

    def budget_and_filter(
        self,
        scored_episodes: Sequence[tuple[Episode | Any, float]],
        *,
        max_budget: int | None = None,
        min_threshold: float | None = None,
    ) -> list[Episode | Any]:
        """Filter episodes by similarity threshold and cap total token consumption.
        
        Takes sorted candidates (highest similarity first), rejects those below
        min_similarity_threshold, and truncates once max_budget is reached.
        """
        threshold = (
            min_threshold if min_threshold is not None else self.min_similarity_threshold
        )
        budget = (
            max_budget if max_budget is not None else self.max_episode_tokens_budget
        )

        accepted: list[Episode | Any] = []
        used_tokens = 0

        for episode, score in scored_episodes:
            if score < threshold:
                continue

            # Estimate or extract tokens
            token_count = getattr(episode, "token_count", 0)
            if not token_count and hasattr(episode, "usage"):
                token_count = episode.usage.get("total_tokens", 0) if isinstance(episode.usage, dict) else 0
            if token_count <= 0:
                # Default estimate based on length if token_count not recorded
                desc = getattr(episode, "task_description", "") or getattr(episode, "goal", "")
                res = getattr(episode, "resolution", "") or getattr(episode, "outcome", "")
                token_count = max(10, (len(desc) + len(res)) // 4)

            if used_tokens + token_count > budget and accepted:
                # Always accept at least one if score exceeds threshold, otherwise cap at budget
                break

            accepted.append(episode)
            used_tokens += token_count

        return accepted

    def calculate_decay_weight(
        self,
        created_timestamp: float,
        current_timestamp: float,
        half_life_days: float | None = None,
    ) -> float:
        """Calculate exponential recency decay weight between 0.0 and 1.0.
        
        Weight = 2^(-delta_days / half_life_days)
        """
        if current_timestamp <= created_timestamp:
            return 1.0
        half_life = half_life_days if half_life_days is not None else self.default_half_life_days
        if half_life <= 0:
            return 1.0
        delta_days = (current_timestamp - created_timestamp) / 86400.0
        return math.pow(2.0, -delta_days / half_life)


__all__ = ["EpisodePolicy"]
