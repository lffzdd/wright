"""How a configured model name maps onto the built-in model tables."""

from __future__ import annotations

import re

# A dated snapshot ("gpt-5-2025-08-07", "claude-haiku-4-5-20251001") belongs to its family.
_SNAPSHOT_SUFFIX = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")


def normalize_model_name(model: str | None) -> str:
    return (model or "").strip().lower()


def name_and_bare(key: str) -> tuple[str, ...]:
    """The name, then without a gateway's vendor prefix ("openai/gpt-5" -> "gpt-5")."""
    return tuple(dict.fromkeys((key, key.rsplit("/", 1)[-1])))


def snapshot_family(name: str) -> str:
    return _SNAPSHOT_SUFFIX.sub("", name)


__all__ = ["name_and_bare", "normalize_model_name", "snapshot_family"]
