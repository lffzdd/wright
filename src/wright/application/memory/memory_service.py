"""MemoryService: the only recall orchestration for core, semantic, and episode memory."""

from __future__ import annotations

from dataclasses import replace

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
    ANCHOR_CURRENT,
    ANCHOR_NONE,
    ANCHOR_READ_ERROR,
    EPISODE_SEARCH_SCOPES,
    EPISODE_STATUSES,
    EVIDENCE_ID_RE,
    EXPLICIT_ORIGIN,
    PROJECT_ID_RE,
    CoreMemory,
    CoreMemoryStoreError,
    EpisodeRecord,
    EpisodeSearchScope,
    EpisodeStoreError,
    EvidenceRef,
    SemanticMemoryConflictError,
    SemanticMemoryNotFoundError,
    SemanticMemoryRecord,
    SemanticMemoryStoreError,
    SemanticMemoryType,
    SourceLocator,
)
from ...domain.policy.memory import (
    CoreMemoryPolicy,
    CoreMemoryUpdateError,
    EpisodePolicy,
    SemanticMemoryPolicy,
    record_in_read_scope,
    scope_denial_message,
)
from .dto import CoreMemoryUpdateDTO, EpisodeViewDTO, MemoryContextDTO
from .projection import (
    EPISODE_RECALL_PREFIX,
    EPISODE_RECALL_SUFFIX,
    SEMANTIC_RECALL_PREFIX,
    budget_episode_manifest,
    budget_semantic_manifest,
    estimate_recall_text,
    recall_overhead_tokens,
    render_episode_for_budget,
    verification_summary,
)

logger = get_logger(__name__)


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
        self._semantic_generation = 0

    @property
    def semantic_generation(self) -> int:
        """Bumps when a semantic write succeeds. Recall uses it to drop stale text."""
        return self._semantic_generation

    def _touch_semantic(self) -> None:
        self._semantic_generation += 1

    def get_core_memory(self, project_id: str = "") -> CoreMemory | None:
        """Compose the current view. A project read failure keeps the global sections."""
        if self._core_memory_store is None:
            return None
        global_record = self._core_memory_store.load_global()
        bound = project_id.strip()
        anchor = ""
        state = ANCHOR_NONE
        if bound:
            try:
                project = self._core_memory_store.load_project(bound)
            except Exception as exc:
                self._diagnose(
                    "core_memory_read failure_type=%s scope=project project_id=%s",
                    type(exc).__name__,
                    bound,
                )
                state = ANCHOR_READ_ERROR
            else:
                anchor = project.project_anchor
                state = ANCHOR_CURRENT
        return CoreMemory(
            persona=global_record.persona,
            human_profile=global_record.human_profile,
            project_anchor=anchor,
            project_id=bound,
            project_anchor_state=state,
        )

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
        core_mem = self._load_core(project_id)
        memories = self._semantic_candidates(project_id)
        selectable, semantic_manifest = self._budget_semantic_manifest(memories)
        candidates = (
            self._episode_candidates(current_task, project_id) if project_id else []
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
                selected_memories = self._take_memories(list(selectable), choice.memory_ids)
                if choice.episodes_usable:
                    selected_episodes = self._take_episodes(candidates, choice.episode_ids)
                else:
                    logger.info(
                        "episode_selector failure_type=%s",
                        choice.failure_type or "invalid_episode_field",
                    )
        elif candidates or memories:
            logger.info("episode_selector failure_type=%s", "unavailable")

        semantic_text = self._render_semantic(memories, selected_memories, project_id)
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
            semantic_generation=self.semantic_generation,
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
        project_id: str,
        memory_id: str = "",
        action: str = "create",
        updatable_ids: tuple[str, ...] = (),
    ) -> tuple[SemanticMemoryRecord | None, str | None]:
        """Write one automatic project memory. Explicit tools do not use this path.

        Updates are limited to ids in this extraction's manifest. A readable
        global memory is not a writable target.
        """
        if not project_id:
            return None, "缺少项目上下文，不能写入项目记忆"
        is_valid, reason = self.semantic_policy.validate(content)
        if not is_valid:
            return None, reason
        if not origin or not source_refs or origin == EXPLICIT_ORIGIN:
            return None, "missing_provenance"
        try:
            if action == "update":
                if not memory_id or memory_id not in updatable_ids:
                    return None, "memory_not_in_manifest"
                current = self.semantic_store.get(memory_id)
                if current.scope != "project" or current.project_id != project_id:
                    return None, "scope_not_writable"
                if current.status != "active":
                    return None, "inactive_not_writable"
                record = self.semantic_store.update(
                    memory_id,
                    expected_revision=current.revision,
                    read_scope="current_project",
                    project_id=project_id,
                    name=name or current.name,
                    description=description or current.description,
                    type_=type_,
                    content=content,
                    origin=origin,
                    source_refs=source_refs,
                    provenance_set=True,
                )
            elif action == "create":
                if memory_id:
                    return None, "create_with_memory_id"
                record = self.semantic_store.create(
                    name=name,
                    description=description or content[:60],
                    type_=type_,
                    content=content,
                    scope="project",
                    project_id=project_id,
                    origin=origin,
                    source_refs=source_refs,
                )
            else:
                return None, "invalid_action"
        except SemanticMemoryConflictError:
            return None, "revision_conflict"
        except SemanticMemoryStoreError as exc:
            return None, str(exc)
        self._touch_semantic()
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
        content: str = "",
        mode: str = "append",
        *,
        project_id: str = "",
    ) -> tuple[CoreMemoryUpdateDTO | None, str | None]:
        """Validate and persist one section, returning the text actually saved.

        ``human_profile`` is written to the global record. ``project_anchor``
        is written only to ``project_id``. The model cannot choose another
        project's file. An empty string is not a clear.
        """
        if self._core_memory_store is None:
            return None, "Core memory store is not configured"

        is_valid, err = self.core_memory_policy.validate_update(section, content, mode)
        if not is_valid:
            return None, err

        normalized_section = section.strip().lower()
        try:
            scope = self.core_memory_policy.scope_for(normalized_section)
        except CoreMemoryUpdateError as exc:
            return None, str(exc)

        if scope == "current_project":
            bound = project_id.strip()
            if not bound:
                return None, "缺少项目上下文，不能更新 project_anchor"
            if PROJECT_ID_RE.fullmatch(bound) is None:
                return None, "非法 project_id"
            try:
                saved = self._update_project_anchor(bound, content, mode)
            except (CoreMemoryUpdateError, CoreMemoryStoreError) as exc:
                return None, str(exc)
            return CoreMemoryUpdateDTO(
                section=normalized_section,
                content=saved,
                scope="current_project",
                project_id=bound,
            ), None

        try:
            saved = self._update_human_profile(content, mode)
        except CoreMemoryUpdateError as exc:
            return None, str(exc)
        return CoreMemoryUpdateDTO(
            section=normalized_section,
            content=saved,
            scope="global",
            project_id="",
        ), None

    def record_memory(
        self,
        *,
        name: str,
        content: str,
        type_: SemanticMemoryType = "project",
        description: str = "",
        scope: str = "project",
        project_id: str = "",
    ) -> tuple[SemanticMemoryRecord | None, str | None]:
        """Validate and persist one explicit semantic memory."""
        return self.create_semantic(
            name=name,
            content=content,
            type_=type_,
            description=description,
            scope=scope,
            project_id=project_id,
        )

    def create_semantic(
        self,
        *,
        name: str,
        content: str,
        type_: SemanticMemoryType = "project",
        description: str = "",
        scope: str = "project",
        project_id: str = "",
    ) -> tuple[SemanticMemoryRecord | None, str | None]:
        """Create one memory. The same display name does not overwrite another."""
        is_valid, reason = self.semantic_policy.validate(content)
        if not is_valid:
            return None, reason
        if type_ not in {"user", "feedback", "project", "reference"}:
            return None, f"非法 memory type: {type_}"
        if scope == "project" and not project_id:
            return None, "缺少项目上下文，不能写入项目记忆"
        if scope not in {"project", "global"}:
            return None, "新建记忆的 scope 只能是 project 或 global"
        try:
            record = self.semantic_store.create(
                name=name,
                description=description or content[:60],
                type_=type_,
                content=content,
                scope=scope,
                project_id="" if scope == "global" else project_id,
                origin=EXPLICIT_ORIGIN,
            )
        except SemanticMemoryStoreError as exc:
            return None, str(exc)
        self._touch_semantic()
        return record, None

    def update_semantic(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        project_id: str = "",
        read_scope: str = "applicable",
        name: str | None = None,
        description: str | None = None,
        type_: SemanticMemoryType | None = None,
        content: str | None = None,
        status: str | None = None,
    ) -> tuple[SemanticMemoryRecord | None, str | None]:
        """Update, deactivate, or reactivate. Content edits do not revive status."""
        if all(
            value is None
            for value in (name, description, type_, content, status)
        ):
            return None, "Provide at least one field to update"
        if type_ is not None and type_ not in {"user", "feedback", "project", "reference"}:
            return None, f"非法 memory type: {type_}"
        if content is not None:
            is_valid, reason = self.semantic_policy.validate(content)
            if not is_valid:
                return None, reason
        try:
            record = self.semantic_store.update(
                memory_id,
                expected_revision=expected_revision,
                read_scope=read_scope,
                project_id=project_id,
                name=name,
                description=description,
                type_=type_,
                content=content,
                status=status,
            )
        except SemanticMemoryNotFoundError as exc:
            return None, str(exc)
        except SemanticMemoryConflictError as exc:
            return None, str(exc)
        except SemanticMemoryStoreError as exc:
            return None, str(exc)
        self._touch_semantic()
        return record, None

    def delete_semantic(
        self,
        memory_id: str,
        *,
        project_id: str = "",
        read_scope: str = "applicable",
    ) -> tuple[SemanticMemoryRecord | None, str | None]:
        try:
            record = self.semantic_store.delete(
                memory_id,
                read_scope=read_scope,
                project_id=project_id,
            )
        except SemanticMemoryStoreError as exc:
            return None, str(exc)
        self._touch_semantic()
        return record, None

    def get_semantic(
        self,
        memory_id: str,
        *,
        project_id: str = "",
        read_scope: str = "applicable",
        include_evidence: bool = False,
    ) -> tuple[SemanticMemoryRecord | None, tuple[EvidenceRead, ...], str | None]:
        """Read one memory, including inactive records inside the requested scope."""
        try:
            record = self.semantic_store.get(memory_id)
        except SemanticMemoryNotFoundError as exc:
            return None, (), str(exc)
        except SemanticMemoryStoreError as exc:
            return None, (), str(exc)
        if not record_in_read_scope(
            scope=record.scope,
            project_id=record.project_id,
            status=record.status,
            read_scope=read_scope,
            current_project_id=project_id,
            include_inactive=True,
        ):
            return None, (), scope_denial_message(
                scope=record.scope,
                status=record.status,
                read_scope=read_scope,
            )
        if not include_evidence:
            return record, (), None
        return record, self._read_locators(record), None

    def search_semantic(
        self,
        query: str = "",
        *,
        type_: str | None = None,
        limit: int = 20,
        project_id: str = "",
        read_scope: str = "applicable",
        include_inactive: bool = False,
    ) -> tuple[list[SemanticMemoryRecord], str | None]:
        """Search inside one scope. An empty match is empty; it does not return the newest rows."""
        try:
            records = list(self.semantic_store.search(
                query,
                limit=limit,
                read_scope=read_scope,
                project_id=project_id,
                include_inactive=include_inactive,
                type_=type_,  # type: ignore[arg-type]
            ))
        except SemanticMemoryStoreError as exc:
            return [], str(exc)
        return [
            record
            for record in records
            if record_in_read_scope(
                scope=record.scope,
                project_id=record.project_id,
                status=record.status,
                read_scope=read_scope,
                current_project_id=project_id,
                include_inactive=include_inactive,
            )
        ], None

    def extraction_manifest(self, project_id: str) -> tuple[str, tuple[str, ...], str]:
        """Updatable project memories, plus a read-only global list.

        Global ids are shown so the model can see them, and are absent from
        the updatable id tuple.
        """
        if not project_id:
            return "(无项目上下文，不能写入)", (), "(无)"
        try:
            writable = list(self.semantic_store.list(
                limit=self.semantic_policy.max_candidates,
                read_scope="current_project",
                project_id=project_id,
                include_inactive=False,
            ))
            readonly = list(self.semantic_store.list(
                limit=50,
                read_scope="global",
                include_inactive=False,
            ))
        except SemanticMemoryStoreError:
            return "(暂无)", (), "(暂无)"
        writable, manifest = self._budget_semantic_manifest(writable)
        readonly_lines = [
            f"- 只读 {record.id}: {record.description or record.name}"
            for record in readonly[:20]
        ]
        return (
            manifest or "(暂无)",
            tuple(record.id for record in writable),
            "\n".join(readonly_lines) or "(暂无)",
        )

    def reproject_semantic(self, previous: MemoryContextDTO) -> MemoryContextDTO:
        """Refresh semantic text for the same selection. Does not call the selector."""
        project_id = previous.project_id
        memories = self._semantic_candidates(project_id)
        selected = self._take_memories(memories, previous.selected_memory_ids)
        semantic_text = self._render_semantic(memories, selected, project_id)
        parts = [part for part in (semantic_text, previous.episode_text) if part]
        return replace(
            previous,
            memories=tuple(selected),
            semantic_text=semantic_text,
            selected_memory_ids=tuple(record.id for record in selected),
            semantic_estimated_tokens=estimate_recall_text(semantic_text) if semantic_text else 0,
            prompt_injection="\n\n".join(parts),
            semantic_generation=self.semantic_generation,
        )

    def _update_human_profile(self, content: str, mode: str) -> str:
        def mutate(record) -> None:
            record.human_profile = self.core_memory_policy.compose_section(
                section="human_profile",
                current=record.human_profile,
                content=content,
                mode=mode,
            )

        return self._core_memory_store.update_global(mutate).human_profile  # type: ignore[union-attr]

    def _update_project_anchor(self, project_id: str, content: str, mode: str) -> str:
        def mutate(record) -> None:
            record.project_anchor = self.core_memory_policy.compose_section(
                section="project_anchor",
                current=record.project_anchor,
                content=content,
                mode=mode,
            )
        return self._core_memory_store.update_project(project_id, mutate).project_anchor  # type: ignore[union-attr]

    def _load_core(self, project_id: str = "") -> CoreMemory | None:
        try:
            return self.get_core_memory(project_id)
        except Exception as exc:
            self._diagnose("core_memory_read failure_type=%s scope=global", type(exc).__name__)
            return None

    def _diagnose(self, message: str, *args: object) -> None:
        try:
            logger.info(message, *args)
        except Exception:
            return

    def _semantic_candidates(self, project_id: str) -> list[SemanticMemoryRecord]:
        limit = self.semantic_policy.max_candidates
        try:
            records = self.semantic_store.list(
                limit=limit,
                read_scope="applicable",
                project_id=project_id,
                include_inactive=False,
            )
        except Exception as exc:
            logger.info("semantic_list failure_type=%s", type(exc).__name__)
            return []
        visible = [
            record
            for record in records
            if record_in_read_scope(
                scope=record.scope,
                project_id=record.project_id,
                status=record.status,
                read_scope="applicable",
                current_project_id=project_id,
                include_inactive=False,
            )
            and self.semantic_policy.validate(record.content)[0]
        ]
        return visible[:limit]

    def _budget_semantic_manifest(
        self,
        records: list[SemanticMemoryRecord],
    ) -> tuple[tuple[SemanticMemoryRecord, ...], str]:
        manifest, kept = budget_semantic_manifest(
            records,
            token_budget=self.semantic_policy.selector_input_token_budget,
        )
        return kept, manifest

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
        """Accept only ids that were actually offered. Budget-dropped ids stay out."""
        by_name = {record.path.name: record for record in memories}
        by_id = {record.id: record for record in memories}
        by_stem = {record.path.stem: record for record in memories}
        chosen: list[SemanticMemoryRecord] = []
        seen: set[str] = set()
        for name in raw_ids:
            if not isinstance(name, str):
                continue
            record = by_id.get(name) or by_name.get(name) or by_stem.get(name.removesuffix(".md"))
            if record is None or record.id in seen:
                continue
            chosen.append(record)
            seen.add(record.id)
            if len(chosen) >= self.semantic_policy.max_selected:
                break
        return chosen

    def _take_episodes(self, candidates, raw_ids: tuple[str, ...]) -> list[EpisodeRecord]:
        by_id = {hit.episode.id: hit.episode for hit in candidates}
        known = [episode_id for episode_id in raw_ids if episode_id in by_id]
        return [by_id[episode_id] for episode_id in self.episode_policy.limit_ids(known)]

    def _render_semantic(
        self,
        candidates: list[SemanticMemoryRecord],
        memories: list[SemanticMemoryRecord],
        project_id: str,
    ) -> str:
        """Render a scoped index and selected bodies inside the semantic budget.

        The index is built from the filtered records. A stale MEMORY.md file
        is not read.
        """
        if not candidates and not memories:
            return ""
        budget = self.semantic_policy.max_semantic_tokens_budget
        if budget <= 0:
            return ""
        scope_note = (
            "范围: 全局 active"
            + (f" 与当前项目 {project_id} 的 active" if project_id else "（当前没有项目，不含项目记忆）")
            + "。不含其他项目和已停用记录。\n"
            "索引由这些记录现算。不要读取未过滤的全库 MEMORY.md。\n"
            "预算未放入的正文用 search_memory 或 get_memory，并带上同样的 scope。\n"
        )
        prefix = SEMANTIC_RECALL_PREFIX.rstrip("\n") + "\n" + scope_note
        suffix = "\n</system-reminder>"
        if estimate_recall_text(prefix + suffix) > budget:
            return ""
        index_lines = ["## 当前范围索引"]
        for record in candidates:
            project = f" project={record.project_id}" if record.scope == "project" else ""
            index_lines.append(
                f"- [{record.type}/{record.scope}{project}] {record.id}: "
                f"{record.description or record.name}"
            )
        kept_index = [index_lines[0]]
        omitted_index = False
        for line in index_lines[1:]:
            trial = prefix + "\n" + "\n".join([*kept_index, line]) + suffix
            if estimate_recall_text(trial) > budget:
                omitted_index = True
                break
            kept_index.append(line)
        if omitted_index:
            notice = "索引已按预算截断。使用 search_memory scope=applicable 继续读取。"
            trial = prefix + "\n" + "\n".join([*kept_index, notice]) + suffix
            if estimate_recall_text(trial) <= budget:
                kept_index.append(notice)
        parts = [prefix, "\n".join(kept_index)]
        if memories:
            blocks = ["## 与本次任务相关的语义记忆"]
            for record in memories:
                label = f"### {record.id} ({record.name}) scope={record.scope}"
                if record.project_id:
                    label += f" project={record.project_id}"
                block = f"{label}\n{record.content}"
                trial = "\n".join([*parts, "\n".join([*blocks, block])]) + suffix
                if estimate_recall_text(trial) > budget:
                    hint = f"未注入 {record.id} 的正文。使用 get_memory 读取。"
                    hinted = "\n".join([*parts, "\n".join([*blocks, hint])]) + suffix
                    if estimate_recall_text(hinted) <= budget:
                        blocks.append(hint)
                    break
                blocks.append(block)
            if len(blocks) > 1:
                parts.append("\n".join(blocks))
        parts.append("</system-reminder>")
        return "\n".join(parts)

    def _read_locators(self, record: SemanticMemoryRecord) -> tuple[EvidenceRead, ...]:
        slots: list[EvidenceRead | None] = []
        refs: list[EvidenceRef] = []
        ref_at: list[int] = []
        for locator in record.resolved_locators():
            early = _unavailable_locator(locator)
            if early is not None:
                slots.append(early)
                continue
            ref = _locator_ref(locator)
            if ref is None:
                slots.append(EvidenceRead(
                    evidence_id=locator.local_id or "source",
                    status="unavailable",
                    kind=locator.kind,
                    reason="unresolved_locator",
                ))
                continue
            slots.append(None)
            ref_at.append(len(slots) - 1)
            refs.append(ref)
        if refs:
            if self.evidence_source is None:
                loaded = [
                    EvidenceRead(
                        evidence_id=ref.id,
                        status="unavailable",
                        kind=ref.kind,
                        reason="source_unavailable",
                    )
                    for ref in refs
                ]
            else:
                loaded = list(self.evidence_source.load(tuple(refs)))
            for index, read in zip(ref_at, loaded, strict=False):
                slots[index] = read
        return tuple(read for read in slots if read is not None)

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


def _unavailable_locator(locator: SourceLocator) -> EvidenceRead | None:
    """A locator without a session is not readable. Do not search another session."""
    if locator.session_id:
        return None
    return EvidenceRead(
        evidence_id=locator.local_id or "source",
        status="unavailable",
        kind=locator.kind,
        reason="unresolved_locator",
    )


def _locator_ref(locator: SourceLocator) -> EvidenceRef | None:
    from ...domain.model.memory.episode import SOURCE_KINDS

    if not locator.local_id or EVIDENCE_ID_RE.fullmatch(locator.local_id) is None:
        return None
    if locator.kind not in SOURCE_KINDS:
        return None
    try:
        return EvidenceRef(
            id=locator.local_id,
            kind=locator.kind,  # type: ignore[arg-type]
            session_id=locator.session_id,
            root_run_id=locator.root_run_id,
            run_id=locator.run_id,
            message_id=locator.message_id,
            tool_call_id=locator.tool_call_id,
            step_id=locator.step_id,
        )
    except EpisodeStoreError:
        return None


__all__ = ["MemoryService"]
