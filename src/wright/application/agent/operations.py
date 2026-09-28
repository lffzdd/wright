"""Read and cooperative-cancel operations for one agent delegation.

The control plane remains the record owner. These methods do not project that
record onto another status machine.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ...domain.model.agent import (
    TERMINAL_AGENT_TASK_STATUSES,
    AgentControlError,
    AgentTaskRecord,
)
from ..execution.identity import (
    ExecutionIdentity,
    ExecutionNotFound,
    ExecutionWaitCancelled,
)


class AgentExecution:
    def __init__(self, control, identity: ExecutionIdentity) -> None:
        self._control = control
        self._identity = identity

    def get(self, agent_task_id: str) -> AgentTaskRecord:
        self._identity.require(agent_task_id, "agent")
        return self._load(agent_task_id)

    def wait(
        self,
        agent_task_id: str,
        *,
        timeout: float,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> AgentTaskRecord:
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        self._identity.require(agent_task_id, "agent")
        deadline = time.monotonic() + timeout
        while True:
            record = self._load(agent_task_id)
            if record.status in TERMINAL_AGENT_TASK_STATUSES:
                return record
            if cancellation_check is not None and cancellation_check():
                raise ExecutionWaitCancelled(agent_task_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return record
            time.sleep(min(0.05, remaining))

    def cancel(self, agent_task_id: str, reason: str) -> tuple[AgentTaskRecord, bool]:
        """Request cooperative cancellation. Return the record and whether it was already terminal."""
        self._identity.require(agent_task_id, "agent")
        current = self._load(agent_task_id)
        already = current.status in TERMINAL_AGENT_TASK_STATUSES
        if not already:
            self._control.request_cancel(agent_task_id, reason[:1_000])
        return self._load(agent_task_id), already

    def tree(self, root_turn_id: str | None) -> list:
        return self._control.tree_summary(root_turn_id)

    def limits(self) -> dict:
        return self._control.config.to_dict()

    def _load(self, agent_task_id: str) -> AgentTaskRecord:
        try:
            return self._control.get(agent_task_id)
        except AgentControlError as exc:
            raise ExecutionNotFound("agent", agent_task_id) from exc


def agent_completion_notice(control, agent_task_id: str) -> dict | None:
    """Model event for a finished delegation. Native status is not rewritten."""
    try:
        record = control.get(agent_task_id)
    except AgentControlError:
        return None
    body = {
        "agent_task_id": record.id[:100],
        "object": "agent",
        "status": record.status,
        "root_turn_id": record.root_turn_id[:180],
        "description": record.task[:500],
        "result": record.result[:2_000],
        "output": "",
        "error": record.error[:1_000],
        "returncode": None,
        "cancel_requested": record.cancel_requested,
        "cancel_reason": record.cancel_reason[:500],
    }
    return {
        "type": "task_notification",
        "follow_up": "Use get_agent, wait_agent, or cancel_agent with agent_task_id.",
        "task": body,
    }


__all__ = ["AgentExecution", "agent_completion_notice"]
