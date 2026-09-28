"""Provider-neutral token usage accumulated on a session or turn."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsageRecord:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


__all__ = ["UsageRecord"]
