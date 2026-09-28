"""Lifecycle orchestration. Persistence and process hooks are injected."""

from __future__ import annotations

from .contracts import (
    BLOCKING_EVENTS,
    HOOKABLE_EVENT_NAMES,
    LIFECYCLE_EVENT_NAMES,
    HookDecision,
    HookExecutionError,
    HookRegistration,
    LifecycleConfigError,
    LifecycleEvent,
    LifecycleEventName,
)
from .manager import LifecycleManager

__all__ = [
    "BLOCKING_EVENTS",
    "HOOKABLE_EVENT_NAMES",
    "LIFECYCLE_EVENT_NAMES",
    "HookDecision",
    "HookExecutionError",
    "HookRegistration",
    "LifecycleConfigError",
    "LifecycleEvent",
    "LifecycleEventName",
    "LifecycleManager",
]
