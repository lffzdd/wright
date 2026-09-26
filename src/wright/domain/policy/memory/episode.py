"""Episode memory policy.

Pure rules for admission, candidate counts, and text-budget selection.
Token costs are supplied by the application; this module does not read
historical ``usage`` and does not score similarity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...model.memory import EpisodeRecord, EpisodeSearchHit
from .user_text import is_blank_user_text, is_substantive_user_text

# Initial retrieval and budget settings for this design. They are explicit
# starting values, not a claim that the numbers are optimal.
DEFAULT_LEXICAL_CANDIDATES = 40
DEFAULT_RECENT_CANDIDATES = 10
DEFAULT_MAX_CANDIDATES = 50
DEFAULT_MAX_SELECTED_EPISODES = 3
DEFAULT_EPISODE_TOKEN_BUDGET = 800
DEFAULT_SINGLE_EPISODE_TOKENS = 400
# Side-request budget for the episode manifest only. Injection stays at
# 800/400. 4096 estimated tokens is about 16k characters, so 50 candidates
# share roughly 320 characters each: enough for an id, a short goal, a
# head/tail outcome, and an error excerpt, without raising the injection caps.
DEFAULT_SELECTOR_INPUT_TOKENS = 4_096


@dataclass(frozen=True)
class EpisodeAdmission:
    """Whether one finished turn is stored, and why it was skipped."""

    keep: bool
    skip_reason: str = ""


@dataclass(frozen=True)
class EpisodePolicy:
    """Ingest, candidate-window, and budget rules for episode memory."""

    lexical_candidate_limit: int = DEFAULT_LEXICAL_CANDIDATES
    recent_candidate_limit: int = DEFAULT_RECENT_CANDIDATES
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_selected_episodes: int = DEFAULT_MAX_SELECTED_EPISODES
    max_episode_tokens_budget: int = DEFAULT_EPISODE_TOKEN_BUDGET
    max_single_episode_tokens: int = DEFAULT_SINGLE_EPISODE_TOKENS
    selector_input_token_budget: int = DEFAULT_SELECTOR_INPUT_TOKENS

    def admission(
        self,
        *,
        tool_count: int,
        agent_count: int,
        user_texts: Sequence[str],
        has_delivered_answer: bool,
    ) -> EpisodeAdmission:
        """Keep work of any outcome. Otherwise keep only a substantive answer.

        Step count and answer length are not value signals. Blank turns and
        pure greetings are skipped. ``user_texts`` must already be real user
        input; recall, hooks, and runtime events are the caller's job to omit.
        """
        if isinstance(tool_count, bool) or not isinstance(tool_count, int) or tool_count < 0:
            return EpisodeAdmission(False, "invalid_input")
        if isinstance(agent_count, bool) or not isinstance(agent_count, int) or agent_count < 0:
            return EpisodeAdmission(False, "invalid_input")
        if tool_count > 0 or agent_count > 0:
            return EpisodeAdmission(True)
        texts = [text for text in user_texts if isinstance(text, str)]
        if not texts or all(is_blank_user_text(text) for text in texts):
            return EpisodeAdmission(False, "blank")
        if not any(is_substantive_user_text(text) for text in texts):
            return EpisodeAdmission(False, "greeting")
        if not has_delivered_answer:
            return EpisodeAdmission(False, "no_answer")
        return EpisodeAdmission(True)

    def should_persist(
        self,
        *,
        tool_count: int,
        agent_count: int,
        user_texts: Sequence[str] = (),
        has_delivered_answer: bool,
    ) -> bool:
        """Compatibility wrapper around :meth:`admission`."""
        return self.admission(
            tool_count=tool_count,
            agent_count=agent_count,
            user_texts=user_texts,
            has_delivered_answer=has_delivered_answer,
        ).keep

    def merge_candidates(
        self,
        ranked: Sequence[EpisodeSearchHit],
        recent: Sequence[EpisodeRecord],
    ) -> list[EpisodeSearchHit]:
        """Union of the top lexical hits and a few recent records.

        Lexical score is only a rank key. Recent records are appended so the
        selector can notice a relevant experience that was phrased differently.
        Overlap is removed. The combined list stops at ``max_candidates``.
        """
        chosen: list[EpisodeSearchHit] = []
        seen: set[str] = set()
        lexical_limit = max(0, self.lexical_candidate_limit)
        recent_limit = max(0, self.recent_candidate_limit)
        cap = max(0, self.max_candidates)
        for hit in ranked:
            if hit.lexical_score <= 0 or hit.episode.id in seen:
                continue
            chosen.append(hit)
            seen.add(hit.episode.id)
            if len(chosen) >= lexical_limit:
                break
        scores = {hit.episode.id: hit.lexical_score for hit in ranked}
        added = 0
        for episode in recent:
            if episode.id in seen:
                continue
            if added >= recent_limit or len(chosen) >= cap:
                break
            chosen.append(EpisodeSearchHit(
                episode=episode,
                lexical_score=float(scores.get(episode.id, 0.0)),
            ))
            seen.add(episode.id)
            added += 1
        return chosen[:cap]

    def limit_ids(self, episode_ids: Sequence[str]) -> list[str]:
        """Dedupe string ids and apply the selection cap. Non-strings are dropped."""
        kept: list[str] = []
        seen: set[str] = set()
        for episode_id in episode_ids:
            if not isinstance(episode_id, str) or not episode_id or episode_id in seen:
                continue
            kept.append(episode_id)
            seen.add(episode_id)
            if len(kept) >= self.max_selected_episodes:
                break
        return kept

    def item_token_limit(self, remaining: int) -> int:
        """How many estimated tokens one more episode may spend."""
        if (
            self.max_episode_tokens_budget <= 0
            or self.max_single_episode_tokens <= 0
            or remaining <= 0
        ):
            return 0
        return min(self.max_single_episode_tokens, remaining)

    def accepts_cost(self, cost: int, remaining: int) -> bool:
        """Whether a rendered summary's estimated cost fits the remaining budget.

        The first item is held to the same rule. A non-positive cost means the
        renderer could not produce a useful summary and is skipped. Historical
        ``usage`` is not an input.
        """
        if self.max_episode_tokens_budget <= 0 or remaining <= 0:
            return False
        if isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
            return False
        if cost > self.max_single_episode_tokens:
            return False
        return cost <= remaining


def is_delivered_answer(final_answer: str | None) -> bool:
    """An answer was delivered when the turn produced non-blank text."""
    return isinstance(final_answer, str) and bool(final_answer.strip())


def has_result_or_verification(episode: EpisodeRecord) -> bool:
    """Tie-break signal: the record has an outcome, an error, or a verification.

    Failure is not a penalty. A failed turn with an error excerpt still counts
    as having something later tasks can reuse. ``ok`` alone does not.
    """
    if episode.outcome.strip():
        return True
    if episode.verification:
        return True
    for tool in episode.tools:
        if str(tool.get("error") or "").strip():
            return True
    for agent in episode.agents:
        if str(agent.get("error") or "").strip():
            return True
    return any(item.error_excerpt.strip() for item in episode.evidence)


__all__ = [
    "DEFAULT_EPISODE_TOKEN_BUDGET",
    "DEFAULT_LEXICAL_CANDIDATES",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MAX_SELECTED_EPISODES",
    "DEFAULT_RECENT_CANDIDATES",
    "DEFAULT_SELECTOR_INPUT_TOKENS",
    "DEFAULT_SINGLE_EPISODE_TOKENS",
    "EpisodeAdmission",
    "EpisodePolicy",
    "has_result_or_verification",
    "is_delivered_answer",
]
