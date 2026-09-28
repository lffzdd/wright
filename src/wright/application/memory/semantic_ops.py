"""Semantic memory writes, reads, and recall projection."""

from __future__ import annotations

from ...core.logger import get_logger
from ...domain.gateway.memory import EvidenceRead
from ...domain.model.memory import (
    EVIDENCE_ID_RE,
    EXPLICIT_ORIGIN,
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
    SemanticMemoryPolicy,
    record_in_read_scope,
    scope_denial_message,
)
from .projection import (
    SEMANTIC_RECALL_PREFIX,
    budget_semantic_manifest,
    estimate_recall_text,
)

logger = get_logger(__name__)

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


class SemanticRecords:
    def __init__(self, store, policy: SemanticMemoryPolicy, evidence_source, on_changed) -> None:
        self.semantic_store = store
        self.semantic_policy = policy
        self.evidence_source = evidence_source
        self._on_changed = on_changed

    def _touch_semantic(self) -> None:
        self._on_changed()

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
            return None, "No project context, so a project memory cannot be written"
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
            return None, f"Invalid memory type: {type_}"
        if scope == "project" and not project_id:
            return None, "No project context, so a project memory cannot be written"
        if scope not in {"project", "global"}:
            return None, "A new memory scope must be project or global"
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
            return None, f"Invalid memory type: {type_}"
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
            return "(no project context, cannot write)", (), "(none)"
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
            return "(none)", (), "(none)"
        writable, manifest = self._budget_semantic_manifest(writable)
        readonly_lines = [
            f"- read-only {record.id}: {record.description or record.name}"
            for record in readonly[:20]
        ]
        return (
            manifest or "(none)",
            tuple(record.id for record in writable),
            "\n".join(readonly_lines) or "(none)",
        )

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
            "Scope: active global memories"
            + (
                f" and active memories for the current project {project_id}"
                if project_id
                else " (no current project, so project memories are excluded)"
            )
            + ". Other projects and inactive records are excluded.\n"
            "This index is computed from those records. Do not read an unfiltered MEMORY.md.\n"
            "Use search_memory or get_memory with the same scope for bodies that did not fit.\n"
        )
        prefix = SEMANTIC_RECALL_PREFIX.rstrip("\n") + "\n" + scope_note
        suffix = "\n</system-reminder>"
        if estimate_recall_text(prefix + suffix) > budget:
            return ""
        index_lines = ["## In-scope index"]
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
            notice = "The index was truncated to the budget. Continue with search_memory scope=applicable."
            trial = prefix + "\n" + "\n".join([*kept_index, notice]) + suffix
            if estimate_recall_text(trial) <= budget:
                kept_index.append(notice)
        parts = [prefix, "\n".join(kept_index)]
        if memories:
            blocks = ["## Semantic memories relevant to this task"]
            for record in memories:
                label = f"### {record.id} ({record.name}) scope={record.scope}"
                if record.project_id:
                    label += f" project={record.project_id}"
                block = f"{label}\n{record.content}"
                trial = "\n".join([*parts, "\n".join([*blocks, block])]) + suffix
                if estimate_recall_text(trial) > budget:
                    hint = f"The body of {record.id} was not injected. Read it with get_memory."
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
