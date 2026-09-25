"""Base value object abstractions."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class ValueObject(ABC):
    """Abstract base class for all domain value objects.

    Value objects are immutable and defined solely by their attributes.
    Two value objects with identical attributes are considered equal.
    """
    pass
