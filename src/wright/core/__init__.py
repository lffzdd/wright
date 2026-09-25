"""Core framework infrastructure: config, logging, paths, processes, and event bus."""

from __future__ import annotations

from .config import AppConfig, get_config
from .event_bus import EventBus, global_bus
from .exceptions import (
    ApplicationError,
    ConfigurationError,
    DomainError,
    EntityNotFoundError,
    InfrastructureError,
    ValidationError,
    WrightError,
)
from .logger import get_logger
from .paths import (
    artifact_dir,
    attachment_dir,
    ensure_project_state,
    project_id,
    project_state_dir,
    session_dir,
    task_db_path,
    trace_dir,
    wright_home,
)

__all__ = [
    "AppConfig",
    "ApplicationError",
    "ConfigurationError",
    "DomainError",
    "EntityNotFoundError",
    "EventBus",
    "InfrastructureError",
    "ValidationError",
    "WrightError",
    "artifact_dir",
    "attachment_dir",
    "ensure_project_state",
    "get_config",
    "get_logger",
    "global_bus",
    "project_id",
    "project_state_dir",
    "session_dir",
    "task_db_path",
    "trace_dir",
    "wright_home",
]
