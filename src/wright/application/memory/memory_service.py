"""MemoryService is the facade tools and the manager call.

Core updates, semantic records, episode queries, and recall selection live in
collaborators. This class keeps one generation counter and does not let tools
open the stores themselves.
"""

from __future__ import annotations

from ...domain.gateway.memory import (
    EvidenceRead,
    IContextSelector,
    ICoreMemoryStore,
    IEpisodeEvidenceSource,
    IEpisodeStore,
    ISemanticMemoryStore,
)
from ...domain.model.memory import (
    CoreMemory,
    EpisodeRecord,
    EpisodeSearchScope,
    SemanticMemoryRecord,
    SemanticMemoryType,
)
from ...domain.policy.memory import (
    CoreMemoryPolicy,
    EpisodePolicy,
    SemanticMemoryPolicy,
)
from .core_ops import CoreMemoryOps
from .dto import CoreMemoryUpdateDTO, EpisodeViewDTO, MemoryContextDTO
from .episode_ops import EpisodeQueries
from .recall_ops import RecallCoordinator
from .semantic_ops import SemanticRecords


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
        self._selector = selector
        self.evidence_source = evidence_source
        self._semantic_generation = 0
        self._core = CoreMemoryOps(core_memory_store, self.core_memory_policy)
        self._semantic = SemanticRecords(
            semantic_store,
            self.semantic_policy,
            evidence_source,
            self._touch_semantic,
        )
        self._episodes = EpisodeQueries(episode_store, self.episode_policy, evidence_source)
        self._recall = RecallCoordinator(
            self._core,
            self._semantic,
            self._episodes,
            selector,
            self.episode_policy,
            lambda: self._semantic_generation,
        )

    @property
    def selector(self):
        return self._selector

    @selector.setter
    def selector(self, value) -> None:
        self._selector = value
        recall = getattr(self, "_recall", None)
        if recall is not None:
            recall.selector = value

    @property
    def semantic_generation(self) -> int:
        """Bumps when a semantic write succeeds. Recall uses it to drop stale text."""
        return self._semantic_generation

    def _touch_semantic(self) -> None:
        self._semantic_generation += 1

    def get_core_memory(self, project_id: str = "") -> CoreMemory | None:
        return self._core.get_core_memory(project_id)

    def update_core_memory(
    self,
    section: str,
    content: str = "",
    mode: str = "append",
    *,
    project_id: str = "",
) -> tuple[CoreMemoryUpdateDTO | None, str | None]:
        return self._core.update_core_memory(section, content, mode, project_id=project_id)

    def prepare_memory_context(
    self,
    current_task: str,
    *,
    project_id: str = "",
    max_memories: int = 200,
) -> MemoryContextDTO:
        return self._recall.prepare_memory_context(current_task, project_id=project_id, max_memories=max_memories)

    def reproject_semantic(self, previous: MemoryContextDTO) -> MemoryContextDTO:
        return self._recall.reproject_semantic(previous)

    def search_episodes(
    self,
    query: str = "",
    *,
    status: str | None = None,
    limit: int = 20,
    scope: EpisodeSearchScope = "current_project",
    project_id: str = "",
) -> tuple[list[EpisodeViewDTO], str | None]:
        return self._episodes.search_episodes(query, status=status, limit=limit, scope=scope, project_id=project_id)

    def get_episode(
    self,
    episode_id: str,
    *,
    include_evidence: bool = False,
) -> tuple[EpisodeViewDTO | None, str | None]:
        return self._episodes.get_episode(episode_id, include_evidence=include_evidence)

    def read_episode_evidence(self, episode: EpisodeRecord) -> list[EvidenceRead]:
        return self._episodes.read_episode_evidence(episode)

    def delete_episode(self, episode_id: str) -> tuple[EpisodeViewDTO | None, str | None]:
        return self._episodes.delete_episode(episode_id)

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
        return self._semantic.record_extracted(name=name, content=content, type_=type_, description=description, origin=origin, source_refs=source_refs, project_id=project_id, memory_id=memory_id, action=action, updatable_ids=updatable_ids)

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
        return self._semantic.record_memory(name=name, content=content, type_=type_, description=description, scope=scope, project_id=project_id)

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
        return self._semantic.create_semantic(name=name, content=content, type_=type_, description=description, scope=scope, project_id=project_id)

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
        return self._semantic.update_semantic(memory_id, expected_revision=expected_revision, project_id=project_id, read_scope=read_scope, name=name, description=description, type_=type_, content=content, status=status)

    def delete_semantic(
    self,
    memory_id: str,
    *,
    project_id: str = "",
    read_scope: str = "applicable",
) -> tuple[SemanticMemoryRecord | None, str | None]:
        return self._semantic.delete_semantic(memory_id, project_id=project_id, read_scope=read_scope)

    def get_semantic(
    self,
    memory_id: str,
    *,
    project_id: str = "",
    read_scope: str = "applicable",
    include_evidence: bool = False,
) -> tuple[SemanticMemoryRecord | None, tuple[EvidenceRead, ...], str | None]:
        return self._semantic.get_semantic(memory_id, project_id=project_id, read_scope=read_scope, include_evidence=include_evidence)

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
        return self._semantic.search_semantic(query, type_=type_, limit=limit, project_id=project_id, read_scope=read_scope, include_inactive=include_inactive)

    def extraction_manifest(self, project_id: str) -> tuple[str, tuple[str, ...], str]:
        return self._semantic.extraction_manifest(project_id)

__all__ = ["MemoryService"]
