"""Provider-neutral token usage accumulated on a session or turn."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsageRecord:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Part of ``prompt_tokens`` the provider served from its prompt cache.
    cache_read_tokens: int = 0
    # Cost of the requests whose model had a known price, in USD.
    cost_usd: float = 0.0
    # Requests with no known price; while any are counted, cost is incomplete.
    unpriced_requests: int = 0

    @property
    def cost_known(self) -> bool:
        return self.unpriced_requests == 0


__all__ = ["UsageRecord"]
