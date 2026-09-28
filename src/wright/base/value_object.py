"""Base value object abstractions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValueObject:
    """Base class for domain value objects.

    Value objects are immutable and defined solely by their attributes.
    Two value objects with identical attributes are considered equal.
    """
