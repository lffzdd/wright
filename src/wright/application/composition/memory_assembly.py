"""Construct the memory store, service, manager, tools and egress guard.

MemoryManager does not create these. Callers that need a working manager use
this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.policy.memory import EpisodePolicy, ExtractPolicy
from ...infrastructure.llm.context_selector import LlmContextSelector
from ...infrastructure.persistence.memory import (
    SessionEvidenceSource,
    SqliteMemoryStore,
    memory_dir,
)
from ..memory.egress import EgressGuard
from ..memory.manager import MemoryManager
from ..memory.service import MemoryService
from ..memory.tools import build_memory_tools


def assemble_memory_manager(
    llm,
    selector_llm=None,
    directory: Path | None = None,
    *,
    episode_policy: EpisodePolicy | None = None,
    extract_policy: ExtractPolicy | None = None,
    session_repository: Any = None,
) -> MemoryManager:
    """Create the store and service, then hand them to a MemoryManager."""
    root = (directory or memory_dir()).expanduser().resolve()
    policy = episode_policy or EpisodePolicy()
    holder: dict[str, MemoryManager] = {}

    def query(messages, **kwargs):
        return holder["manager"]._query(messages, **kwargs)

    service = MemoryService(
        SqliteMemoryStore(root),
        selector=LlmContextSelector(query),
        evidence_source=SessionEvidenceSource(session_repository) if session_repository is not None else None,
        episode_policy=policy,
    )
    manager = MemoryManager(
        llm,
        service,
        root,
        selector_llm=selector_llm,
        episode_policy=policy,
        extract_policy=extract_policy,
        session_repository=session_repository,
    )
    holder["manager"] = manager
    return manager


def assemble_memory_service(directory: Path | None = None) -> MemoryService:
    """Store only, for the memory console. No selector and no evidence reader."""
    root = (directory or memory_dir()).expanduser().resolve()
    return MemoryService(SqliteMemoryStore(root))


def memory_tools(manager: MemoryManager):
    return build_memory_tools(manager)


def egress_guard(manager: MemoryManager) -> EgressGuard:
    """The paraphrase judge uses the same cheap side model as recall."""
    return EgressGuard(manager.service, judge=manager._side_query)


__all__ = ["assemble_memory_manager", "assemble_memory_service", "egress_guard", "memory_tools"]
