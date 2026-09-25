"""Global domain and application exceptions hierarchy."""

from __future__ import annotations

from ..common.errors import (
    ApplicationError,
    ConfigurationError,
    DomainError,
    EntityNotFoundError,
    InfrastructureError,
    ValidationError,
    WrightError,
)

__all__ = [
    "ApplicationError",
    "ConfigurationError",
    "DomainError",
    "EntityNotFoundError",
    "InfrastructureError",
    "ValidationError",
    "WrightError",
]
