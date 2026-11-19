"""Normalize provider usage objects into the domain usage record."""

from __future__ import annotations

from typing import Any

from .usage import UsageRecord


def _field(source: Any, name: str) -> Any:
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _cache_read_tokens(usage: Any) -> int:
    """Cached prompt tokens, wherever the provider reports them.

    Chat Completions: ``prompt_tokens_details.cached_tokens``; Responses:
    ``input_tokens_details.cached_tokens``; DeepSeek: ``prompt_cache_hit_tokens``;
    Anthropic: ``cache_read_input_tokens``.
    """
    for details in ("prompt_tokens_details", "input_tokens_details"):
        cached = _count(_field(_field(usage, details), "cached_tokens"))
        if cached:
            return cached
    return _count(_field(usage, "prompt_cache_hit_tokens")) or _count(
        _field(usage, "cache_read_input_tokens")
    )


def usage_from_provider(usage: Any, *, cost_usd: float | None = None) -> UsageRecord:
    """Accept a dict or an SDK object. Missing totals fall back to the sum.

    ``prompt_tokens`` always includes cached tokens. Anthropic reports
    ``input_tokens`` without cache reads and writes, so those are added back.
    """
    if isinstance(usage, UsageRecord):
        return usage
    prompt_tokens = _count(_field(usage, "prompt_tokens") or _field(usage, "input_tokens"))
    completion_tokens = _count(
        _field(usage, "completion_tokens") or _field(usage, "output_tokens")
    )
    cache_read = _cache_read_tokens(usage)
    anthropic_shape = _field(usage, "cache_read_input_tokens") is not None or _field(
        usage, "cache_creation_input_tokens"
    ) is not None
    if anthropic_shape and _field(usage, "prompt_tokens") is None:
        prompt_tokens += cache_read + _count(_field(usage, "cache_creation_input_tokens"))
    total_tokens = _field(usage, "total_tokens")
    if total_tokens is None or anthropic_shape:
        total_tokens = prompt_tokens + completion_tokens
    return UsageRecord(
        prompt_tokens,
        completion_tokens,
        _count(total_tokens),
        cache_read_tokens=min(cache_read, prompt_tokens),
        cost_usd=cost_usd or 0.0,
        unpriced_requests=0 if cost_usd is not None else 1,
    )


__all__ = ["usage_from_provider"]
