"""SQLite-backed durable autonomy store."""

from __future__ import annotations

from ._automations import _AutomationsMixin
from ._base import AutonomyNotFoundError, AutonomyStoreError, _StoreBase
from ._commands import _CommandsMixin
from ._history import _HistoryMixin
from ._interactions import _InteractionsMixin
from ._migrations import _MigrationsMixin
from ._runs import _RunsMixin
from ._scheduling import _SchedulingMixin
from ._tool_executions import _ToolExecutionsMixin


class AutonomyStore(
    _MigrationsMixin,
    _AutomationsMixin,
    _CommandsMixin,
    _InteractionsMixin,
    _ToolExecutionsMixin,
    _HistoryMixin,
    _SchedulingMixin,
    _RunsMixin,
    _StoreBase,
):
    """SQLite-backed store for durable automations, runs, and commands."""


__all__ = [
    "AutonomyNotFoundError",
    "AutonomyStore",
    "AutonomyStoreError",
]
