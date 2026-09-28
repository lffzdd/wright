"""Normalize provider usage objects into the domain usage record."""

from __future__ import annotations

from typing import Any

from ...domain.model.llm.usage import UsageRecord


def usage_from_provider(usage: Any) -> UsageRecord:
    """Accept a dict or an SDK object. Missing totals fall back to the sum."""
    if isinstance(usage, UsageRecord):
        return usage
    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        total_tokens = usage.get("total_tokens")
    else:
        prompt_tokens = int(
            getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
        )
        completion_tokens = int(
            getattr(usage, "completion_tokens", 0)
            or getattr(usage, "output_tokens", 0)
            or 0
        )
        total_tokens = getattr(usage, "total_tokens", None)
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    return UsageRecord(prompt_tokens, completion_tokens, int(total_tokens or 0))


__all__ = ["usage_from_provider"]
