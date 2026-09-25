"""Base primitives and abstractions for domain modeling."""

from __future__ import annotations

from .aggregate_root import AggregateRoot
from .base_entity import BaseEntity
from .value_object import ValueObject

__all__ = [
    "AggregateRoot",
    "BaseEntity",
    "ValueObject",
]
