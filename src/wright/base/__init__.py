"""Base primitives and abstractions for domain modeling."""

from __future__ import annotations

from .base_entity import BaseEntity
from .value_object import ValueObject

__all__ = [
    "BaseEntity",
    "ValueObject",
]
