"""Episode search, evidence reads, and episode recall projection."""

from __future__ import annotations

from ...core.logger import get_logger
from ...domain.gateway.memory import EvidenceRead
from ...domain.model.memory import (
    EPISODE_SEARCH_SCOPES,
    EPISODE_STATUSES,
    EpisodeRecord,
    EpisodeSearchScope,
    EpisodeStoreError,
)
from ...domain.policy.memory import (
    EpisodePolicy,
)
from .dto import EpisodeViewDTO
from .projection import (
    EPISODE_RECALL_PREFIX,
    EPISODE_RECALL_SUFFIX,
    estimate_recall_text,
    recall_overhead_tokens,
    render_episode_for_budget,
    verification_summary,
)

logger = get_logger(__name__)

class EpisodeQueries:
    def __init__(self, store, policy: EpisodePolicy, evidence_source) -> None:
        self.episode_store = store
        self.episode_policy = policy
        self.evidence_source = evidence_source

    def search_episodes(
        self,
        query: str = "",
        *,
        status: str | None = None,
        limit: int = 20,
        scope: EpisodeSearchScope = "current_project",
        project_id: str = "",
    ) -> tuple[list[EpisodeViewDTO], str | None]:
        if scope not in EPISODE_SEARCH_SCOPES:
            return [], "episode scope 非法"
        if status is not None and status not in EPISODE_STATUSES:
            return [], "episode status 非法"
        try:
            hits = self.episode_store.search(
                query,
                status=status,  # type: ignore[arg-type]
                limit=limit,
                scope=scope,
                project_id=project_id,
            )
        except EpisodeStoreError as exc:
            return [], str(exc)
        return [self._view(hit.episode, lexical_score=hit.lexical_score) for hit in hits], None

    def get_episode(
        self,
        episode_id: str,
        *,
        include_evidence: bool = False,
    ) -> tuple[EpisodeViewDTO | None, str | None]:
        """Read one episode. Evidence text is loaded only when requested."""
        try:
            episode = self.episode_store.get(episode_id)
        except EpisodeStoreError as exc:
            return None, str(exc)
        reads: tuple[EvidenceRead, ...] = ()
        if include_evidence:
            reads = tuple(self.read_episode_evidence(episode))
        return self._view(episode, include_record=True, evidence_reads=reads), None

    def read_episode_evidence(self, episode: EpisodeRecord) -> list[EvidenceRead]:
        """Read only locators stored on this episode. One session load per id."""
        if not episode.evidence:
            return []
        if self.evidence_source is None:
            return [
                EvidenceRead(
                    evidence_id=item.id,
                    status="unavailable",
                    kind=item.kind,
                    reason="source_unavailable",
                )
                for item in episode.evidence
            ]
        registered = {item.id: item for item in episode.evidence}
        return list(self.evidence_source.load(tuple(registered.values())))

    def delete_episode(self, episode_id: str) -> tuple[EpisodeViewDTO | None, str | None]:
        try:
            episode = self.episode_store.delete(episode_id)
        except EpisodeStoreError as exc:
            return None, str(exc)
        return self._view(episode), None

    def _episode_candidates(self, task: str, project_id: str):
        try:
            lexical_limit = self.episode_policy.lexical_candidate_limit
            recent_limit = self.episode_policy.recent_candidate_limit
            ranked = (
                self.episode_store.search(
                    task,
                    limit=min(100, lexical_limit),
                    scope="current_project",
                    project_id=project_id,
                )
                if lexical_limit > 0
                else []
            )
            recent = (
                self.episode_store.recent(project_id=project_id, limit=recent_limit)
                if recent_limit > 0
                else []
            )
        except Exception as exc:
            logger.info("episode_search failure_type=%s", type(exc).__name__)
            return []
        return self.episode_policy.merge_candidates(ranked, recent)

    def _take_episodes(self, candidates, raw_ids: tuple[str, ...]) -> list[EpisodeRecord]:
        by_id = {hit.episode.id: hit.episode for hit in candidates}
        known = [episode_id for episode_id in raw_ids if episode_id in by_id]
        return [by_id[episode_id] for episode_id in self.episode_policy.limit_ids(known)]

    def _render_episodes(
        self, episodes: list[EpisodeRecord]
    ) -> tuple[str, int, tuple[str, ...], tuple[str, ...]]:
        policy = self.episode_policy
        if policy.max_episode_tokens_budget <= 0 or not episodes:
            return "", 0, tuple(episode.id for episode in episodes), ()
        overhead = recall_overhead_tokens()
        if overhead > policy.max_episode_tokens_budget:
            return "", 0, tuple(episode.id for episode in episodes), ()
        remaining = policy.max_episode_tokens_budget - overhead
        chunks: list[tuple[str, str]] = []
        skipped: list[str] = []
        for episode in episodes:
            limit = policy.item_token_limit(remaining)
            text = render_episode_for_budget(episode, token_limit=limit)
            cost = estimate_recall_text(text) if text else 0
            if text is None or not policy.accepts_cost(cost, remaining):
                skipped.append(episode.id)
                continue
            chunks.append((episode.id, text))
            remaining -= cost
        if not chunks:
            return "", 0, tuple(skipped), ()
        body = EPISODE_RECALL_PREFIX + "\n\n".join(text for _, text in chunks) + EPISODE_RECALL_SUFFIX
        total = estimate_recall_text(body)
        while chunks and total > policy.max_episode_tokens_budget:
            dropped_id, _text = chunks.pop()
            skipped.append(dropped_id)
            if not chunks:
                return "", 0, tuple(skipped), ()
            body = (
                EPISODE_RECALL_PREFIX
                + "\n\n".join(text for _, text in chunks)
                + EPISODE_RECALL_SUFFIX
            )
            total = estimate_recall_text(body)
        return body, total, tuple(skipped), tuple(episode_id for episode_id, _text in chunks)

    def _view(
        self,
        episode: EpisodeRecord,
        *,
        lexical_score: float | None = None,
        include_record: bool = False,
        evidence_reads: tuple[EvidenceRead, ...] = (),
    ) -> EpisodeViewDTO:
        source = episode.project_id
        return EpisodeViewDTO(
            id=episode.id,
            project_id=episode.project_id,
            project_source=source,
            created_at=episode.created_at,
            goal=episode.goal,
            status=episode.status,
            outcome=episode.outcome,
            termination_reason=episode.termination_reason,
            verification_summary=verification_summary(episode),
            lexical_score=lexical_score,
            record=episode if include_record else None,
            evidence_reads=evidence_reads,
        )
