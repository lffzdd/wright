"""Context retention and token budget policies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextPolicy:
    """Policy rules governing when context compaction and window sliding trigger."""

    trigger_ratio: float = 0.8
    target_ratio: float = 0.5
    min_recent_turns: int = 4

    def should_compact(self, current_tokens: int, limit: int) -> bool:
        """Return True if token usage exceeds the compaction trigger threshold."""
        if limit <= 0:
            return False
        return (current_tokens / limit) >= self.trigger_ratio

    def target_tokens(self, limit: int) -> int:
        """Calculate target token count after compaction."""
        return int(limit * self.target_ratio)


TokenBudgetPolicy = ContextPolicy

__all__ = [
    "ContextPolicy",
    "TokenBudgetPolicy",
]
