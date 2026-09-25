"""Common error hierarchy for domain, application, and infrastructure."""

from __future__ import annotations


class WrightError(Exception):
    """Base exception for all wright-specific errors."""


class DomainError(WrightError):
    """Raised when a domain rule or invariant is violated."""


class ApplicationError(WrightError):
    """Raised when an application use-case or workflow fails."""


class InfrastructureError(WrightError):
    """Raised when an external adapter, storage, or transport fails."""


class ConfigurationError(WrightError):
    """Raised when system or tool configuration is invalid."""


class ValidationError(DomainError):
    """Raised when input validation fails in domain models."""


class EntityNotFoundError(DomainError):
    """Raised when an entity requested from a repository cannot be found."""
