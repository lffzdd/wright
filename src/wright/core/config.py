"""Global application and runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..common.constants import (
    DEFAULT_CONTEXT_LIMIT,
    DEFAULT_MAX_STEPS,
    DEFAULT_MODEL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
)


@dataclass
class AppConfig:
    """Runtime application configuration loaded from environment or flags."""

    default_model: str = field(
        default_factory=lambda: os.getenv("WRIGHT_DEFAULT_MODEL", DEFAULT_MODEL)
    )
    context_limit: int = field(
        default_factory=lambda: int(os.getenv("WRIGHT_CONTEXT_LIMIT", str(DEFAULT_CONTEXT_LIMIT)))
    )
    temperature: float = field(
        default_factory=lambda: float(os.getenv("WRIGHT_TEMPERATURE", str(DEFAULT_TEMPERATURE)))
    )
    max_steps: int = field(
        default_factory=lambda: int(os.getenv("WRIGHT_MAX_STEPS", str(DEFAULT_MAX_STEPS)))
    )
    timeout: float = field(
        default_factory=lambda: float(os.getenv("WRIGHT_TIMEOUT", str(DEFAULT_TIMEOUT)))
    )
    poll_interval: float = field(
        default_factory=lambda: float(os.getenv("WRIGHT_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL)))
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("WRIGHT_LOG_LEVEL", "INFO")
    )


def get_config() -> AppConfig:
    """Return default configuration instance."""
    return AppConfig()
