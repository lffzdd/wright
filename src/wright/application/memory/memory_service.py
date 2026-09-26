"""MemoryService: the only recall orchestration for core, semantic, and episode memory."""

from __future__ import annotations

from ...core.logger import get_logger
from ...domain.gateway.memory import (
    EvidenceRead,
    IContextSelector,
    ICoreMemoryStore,
    IEpisodeEvidenceSource,
    IEpisodeStore,
    ISemanticMemoryStore,
)
from ...domain.model.memory import (
    EPISODE_SEARCH_SCOPES,
    EPISODE_STATUSES,
    CoreMemory,
    EpisodeRecord,
    EpisodeSearchScope,
    EpisodeStoreError,
    SemanticMemoryRecord,
    SemanticMemoryStoreError,
    SemanticMemoryType,
)
from ...domain.policy.memory import (
    CoreMemoryPolicy,
    CoreMemoryUpdateError,
    EpisodePolicy,
    SemanticMemoryPolicy,
)
from .dto import CoreMemoryUpdateDTO, EpisodeViewDTO, MemoryContextDTO
from .projection import (
    EPISODE_RECALL_PREFIX,
    EPISODE_RECALL_SUFFIX,
    SEMANTIC_RECALL_PREFIX,
    budget_episode_manifest,
    estimate_recall_text,
    recall_overhead_tokens,
    render_episode_for_budget,
    verification_summary,
)

logger = get_logger(__name__)

_MAX_SELECTED_MEMORIES = 5


class MemoryService:
    """Orchestrates core memory, semantic memory, and episode records."""

    def __init__(
        self,
        semantic_store: ISemanticMemoryStore,
        episode_store: IEpisodeStore,
        core_memory_store: ICoreMemoryStore | None = None,
        semantic_policy: SemanticMemoryPolicy | None = None,
        episode_policy: EpisodePolicy | None = None,
        core_memory_policy: CoreMemoryPolicy | None = None,
        selector: IContextSelector | None = None,
        evidence_source: IEpisodeEvidenceSource | None = None,
    ) -> None:
        self.semantic_store = semantic_store
        self.episode_store = episode_store
        self._core_memory_store = core_memory_store
        self.semantic_policy = semantic_policy or SemanticMemoryPolicy()
        self.episode_policy = episode_policy or EpisodePolicy()
        self.core_memory_policy = core_memory_policy or CoreMemoryPolicy()
        self.selector = selector
        self.evidence_source = evidence_source

    def get_core_memory(self) -> CoreMemory | None:
        """Read pinned core memory, or return None when storage is not configured."""
        if self._core_memory_store is None:
            return None
        return self._core_memory_store.load()

    def prepare_memory_context(
        self,
        current_task: str,
        *,
        project_id: str = "",
        max_memories: int = 200,
    ) -> MemoryContextDTO:
        """Select semantic and episode recall for one task.

        Core memory is returned for callers and is injected by the system-prompt
        projection, not by this recall block. A selector failure leaves episodes
        empty and still returns a readable semantic index.
        """
        core_mem = self._load_core()
        memories = self._semantic_candidates(max_memories)
        index = self._read_index()
        candidates = (
            self._episode_candidates(current_task, project_id) if project_id else []
        )
        selected_memories: list[SemanticMemoryRecord] = []
        selected_episodes: list[EpisodeRecord] = []
        selector_attempted = False
        selector_failed = False
        selector_failure_type = ""
        if self.selector is not None and (memories or candidates or index):
            selector_attempted = True
            episode_manifest, _dropped = budget_episode_manifest(
                [hit.episode for hit in candidates],
                token_budget=self.episode_policy.selector_input_token_budget,
            )
            choice = self.selector.select(
                task=current_task,
                semantic_manifest=self._semantic_manifest(memories),
                episode_manifest=episode_manifest,
            )
            selector_failed = bool(choice.failed)
            selector_failure_type = choice.failure_type
            if choice.failed:
                logger.info("episode_selector failure_type=%s", choice.failure_type or "invalid")
            else:
                selected_memories = self._take_memories(memories, choice.memory_ids)
                if choice.episodes_usable:
                    selected_episodes = self._take_episodes(candidates, choice.episode_ids)
                else:
                    logger.info(
                        "episode_selector failure_type=%s",
                        choice.failure_type or "invalid_episode_field",
                    )
        elif candidates or memories:
            logger.info("episode_selector failure_type=%s", "unavailable")

        semantic_text = self._render_semantic(index, selected_memories)
        episode_text, episode_cost, skipped, rendered_ids = self._render_episodes(
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
            selected_memory_ids=tuple(record.path.name for record in selected_memories),
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
        )

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

    def record_extracted(
        self,
        *,
        name: str,
        content: str,
        type_: SemanticMemoryType,
        description: str = "",
        origin: str,
        source_refs: tuple[str, ...],
        memory_id: str = "",
        action: str = "create",
    ) -> tuple[SemanticMemoryRecord | None, str | None]:
        """Write one automatic memory. Explicit tools do not use this path."""
        is_valid, reason = self.semantic_policy.validate(content)
        if not is_valid:
            return None, reason
        if not origin or not source_refs:
            return None, "missing_provenance"
        try:
            if action == "update":
                if not memory_id:
                    return None, "missing_memory_id"
                current = self.semantic_store.get(memory_id)
                record = self.semantic_store.save(
                    name=name or current.name,
                    description=description or current.description,
                    type_=type_,
                    content=content,
                    origin=origin,
                    source_refs=source_refs,
                    memory_id=memory_id,
                )
            else:
                record = self.semantic_store.save(
                    name=name,
                    description=description or content[:60],
                    type_=type_,
                    content=content,
                    origin=origin,
                    source_refs=source_refs,
                )
        except SemanticMemoryStoreError as exc:
            return None, str(exc)
        return record, None

    def delete_episode(self, episode_id: str) -> tuple[EpisodeViewDTO | None, str | None]:
        try:
            episode = self.episode_store.delete(episode_id)
        except EpisodeStoreError as exc:
            return None, str(exc)
        return self._view(episode), None

    def update_core_memory(
        self,
        section: str,
        content: str,
        mode: str = "append",
    ) -> tuple[CoreMemoryUpdateDTO | None, str | None]:
        """Validate and persist an update, returning its saved content or an error."""
        if self._core_memory_store is None:
            return None, "Core memory store is not configured"

        is_valid, err = self.core_memory_policy.validate_update(section, content, mode)
        if not is_valid:
            return None, err

        normalized_section = section.strip().lower()

        def mutate(core_mem: CoreMemory) -> None:
            self.core_memory_policy.apply_update(core_mem, normalized_section, content, mode)

        try:
            core_mem = self._core_memory_store.update(mutate)
        except CoreMemoryUpdateError as exc:
            return None, str(exc)
        final_text = (
            core_mem.human_profile
            if normalized_section == "human_profile"
            else core_mem.project_anchor
        )
        return CoreMemoryUpdateDTO(section=normalized_section, content=final_text), None

    def record_memory(
        self,
        *,
        name: str,
        content: str,
        type_: SemanticMemoryType = "project",
        description: str = "",
    ) -> tuple[SemanticMemoryRecord | None, str | None]:
        """Validate and persist one semantic memory."""
        is_valid, reason = self.semantic_policy.validate(content)
        if not is_valid:
            return None, reason
        try:
            record = self.semantic_store.save(
                name=name,
                description=description or content[:60],
                type_=type_,
                content=content,
            )
        except SemanticMemoryStoreError as exc:
            return None, str(exc)
        return record, None

    def _load_core(self) -> CoreMemory | None:
        try:
            return self.get_core_memory()
        except Exception as exc:
            logger.info("core_memory_read failure_type=%s", type(exc).__name__)
            return None

    def _read_index(self) -> str:
        try:
            return self.semantic_store.read_index()
        except Exception as exc:
            logger.info("semantic_index failure_type=%s", type(exc).__name__)
            return ""

    def _semantic_candidates(self, limit: int) -> list[SemanticMemoryRecord]:
        try:
            records = self.semantic_store.list(limit=limit)
        except Exception as exc:
            logger.info("semantic_list failure_type=%s", type(exc).__name__)
            return []
        return [
            record
            for record in records
            if self.semantic_policy.validate(record.content)[0]
        ]

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

    def _take_memories(
        self,
        memories: list[SemanticMemoryRecord],
        raw_ids: tuple[str, ...],
    ) -> list[SemanticMemoryRecord]:
        by_name = {record.path.name: record for record in memories}
        by_id = {record.id: record for record in memories}
        chosen: list[SemanticMemoryRecord] = []
        seen: set[str] = set()
        for name in raw_ids:
            if not isinstance(name, str):
                continue
            record = by_name.get(name) or by_id.get(name.removesuffix(".md"))
            if record is None or record.id in seen:
                continue
            chosen.append(record)
            seen.add(record.id)
            if len(chosen) >= _MAX_SELECTED_MEMORIES:
                break
        return chosen

    def _take_episodes(self, candidates, raw_ids: tuple[str, ...]) -> list[EpisodeRecord]:
        by_id = {hit.episode.id: hit.episode for hit in candidates}
        known = [episode_id for episode_id in raw_ids if episode_id in by_id]
        return [by_id[episode_id] for episode_id in self.episode_policy.limit_ids(known)]

    def _render_semantic(self, index: str, memories: list[SemanticMemoryRecord]) -> str:
        if not index and not memories:
            return ""
        parts = [SEMANTIC_RECALL_PREFIX.rstrip("\n")]
        if index:
            parts.append("\n## 语义记忆索引 (MEMORY.md)\n" + index)
        if memories:
            blocks = [
                f"### {record.path.name}\n{record.content}"
                for record in memories
            ]
            parts.append("\n## 与本次任务相关的语义记忆\n" + "\n\n".join(blocks))
        parts.append("</system-reminder>")
        return "\n".join(parts)

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

    def _semantic_manifest(self, memories: list[SemanticMemoryRecord]) -> str:
        return "\n".join(
            f"- [{record.type}] {record.path.name}: {record.description or record.name}"
            for record in memories
        )

    def _view(
        self,
        episode: EpisodeRecord,
        *,
        lexical_score: float | None = None,
        include_record: bool = False,
        evidence_reads: tuple[EvidenceRead, ...] = (),
    ) -> EpisodeViewDTO:
        source = episode.project_id or "legacy"
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


__all__ = ["MemoryService"]
