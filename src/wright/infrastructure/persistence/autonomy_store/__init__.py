"""Shared SQLite store for job scheduling, commands, interactions, and tool logs.

Scheduling application code depends on the ports in
``application.scheduling.contracts``. This class is the one adapter that
implements those ports and the command ledger together, so a definition, a
run, and its tool log can still commit in one transaction.
"""

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
    """SQLite store shared by job scheduling, commands, interactions, and history."""


__all__ = [
    "AutonomyNotFoundError",
    "AutonomyStore",
    "AutonomyStoreError",
]
