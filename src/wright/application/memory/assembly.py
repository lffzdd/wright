"""Construct memory stores, the selector, and memory tools.

MemoryManager does not create these. Callers that need a working manager
use this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.policy.memory import EpisodePolicy, SemanticExtractPolicy
from ...infrastructure.llm.context_selector import LlmContextSelector
from ...infrastructure.persistence.memory import (
    EpisodeStore,
    FileCoreMemoryStore,
    SemanticMemoryStore,
    memory_dir,
)
from ...infrastructure.tools.memory import (
    build_core_memory_tools,
    build_episode_tools,
    build_memory_tools,
)
from .manager import MemoryManager
from .memory_service import MemoryService


def assemble_memory_manager(
    llm,
    selector_llm=None,
    directory: Path | None = None,
    *,
    episode_policy: EpisodePolicy | None = None,
    semantic_policy: SemanticExtractPolicy | None = None,
    session_repository: Any = None,
) -> MemoryManager:
    """Create the stores and service, then hand them to a MemoryManager."""
    root = (directory or memory_dir()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    policy = episode_policy or EpisodePolicy()
    holder: dict[str, MemoryManager] = {}

    def query(messages, **kwargs):
        return holder["manager"]._query(messages, **kwargs)

    evidence_source = None
    if session_repository is not None:
        from ...infrastructure.persistence.memory.evidence import SessionEvidenceSource

        evidence_source = SessionEvidenceSource(session_repository)
    service = MemoryService(
        semantic_store=SemanticMemoryStore(root),
        episode_store=EpisodeStore(root),
        core_memory_store=FileCoreMemoryStore(root),
        episode_policy=policy,
        selector=LlmContextSelector(query),
        evidence_source=evidence_source,
    )
    manager = MemoryManager(
        llm,
        service,
        root,
        selector_llm=selector_llm,
        episode_policy=policy,
        semantic_policy=semantic_policy,
        session_repository=session_repository,
    )
    holder["manager"] = manager
    return manager


def memory_tools(manager: MemoryManager):
    """Tools that reach storage only through the manager's MemoryService."""
    return [
        *build_core_memory_tools(
            service_reader=lambda: manager.service,
            project_id_reader=lambda: manager.current_project_id,
        ),
        *build_memory_tools(
            service_reader=lambda: manager.service,
            project_id_reader=lambda: manager.current_project_id,
        ),
        *build_episode_tools(manager.service, project_id=manager.current_project_id),
    ]
