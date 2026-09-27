"""Domain policies for agent memory (core, semantic, episode)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .core import CoreMemoryPolicy, CoreMemoryUpdateError
from .episode import (
    EpisodeAdmission,
    EpisodePolicy,
    has_result_or_verification,
    is_delivered_answer,
)
from .semantic import (
    ExtractDecision,
    ExtractSignal,
    ProvenanceReview,
    SemanticExtractPolicy,
    SemanticMemoryPolicy,
    bounded_safe_text,
    is_safe_memory,
    normalize_stored_scope,
    record_in_read_scope,
    review_provenance,
    scope_denial_message,
)
from .user_text import (
    is_substantive_user_text,
    is_trivial_user_text,
    normalize_user_text,
)


@dataclass(frozen=True)
class MemoryPolicy:
    """Unified policy facade for memory management."""

    episode: EpisodePolicy = field(default_factory=EpisodePolicy)
    semantic: SemanticMemoryPolicy = field(default_factory=SemanticMemoryPolicy)
    extract: SemanticExtractPolicy = field(default_factory=SemanticExtractPolicy)
    core: CoreMemoryPolicy = field(default_factory=CoreMemoryPolicy)


__all__ = [
    "CoreMemoryPolicy",
    "CoreMemoryUpdateError",
    "EpisodeAdmission",
    "EpisodePolicy",
    "ExtractDecision",
    "ExtractSignal",
    "MemoryPolicy",
    "ProvenanceReview",
    "SemanticExtractPolicy",
    "SemanticMemoryPolicy",
    "bounded_safe_text",
    "has_result_or_verification",
    "is_delivered_answer",
    "is_safe_memory",
    "is_substantive_user_text",
    "is_trivial_user_text",
    "normalize_stored_scope",
    "normalize_user_text",
    "record_in_read_scope",
    "review_provenance",
    "scope_denial_message",
]
