"""Usage accounting and summary notifications for Agent runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...core.logger import get_logger
from ...domain.model.llm import UsageRecord
from ...domain.model.session import Session
from ..session.events import SessionEvents

if TYPE_CHECKING:
    from ...domain.model.session import TurnRecord

logger = get_logger(__name__)


class AgentUsageTracker:
    """Manages token accounting and user-facing usage reports."""

    def __init__(
        self,
        session_state: Session,
        ui: SessionEvents,
        *,
        usage_observer: Callable[[UsageRecord], None] | None = None,
        has_live_agent_tasks: Callable[[str], bool] | None = None,
    ) -> None:
        self.session_state = session_state
        self.ui = ui
        self.usage_observer = usage_observer
        self.has_live_agent_tasks = has_live_agent_tasks

    def record_auxiliary_usage(self, usage: UsageRecord) -> None:
        self.session_state.add_usage(usage)
        self.notify_usage(usage)

    def render_usage_summary(self) -> None:
        if self.has_live_agent_tasks is not None and self.has_live_agent_tasks(
            self.session_state.agent_root_turn_id
        ):
            return
        usage = self.session_state.task_usage()
        self.ui.on_usage_summary(
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
        )

    def record_usage_for_turn(
        self,
        turn_record: TurnRecord,
        usage_record: UsageRecord,
        transient_plan_tokens: int = 0,
    ) -> None:
        self.session_state.record_usage_for_turn(turn_record, usage_record)
        self.notify_usage(usage_record)

    def notify_usage(self, usage_record: UsageRecord) -> None:
        if self.usage_observer is not None:
            try:
                self.usage_observer(usage_record)
            except Exception:
                # 计量旁路失败不能破坏当前消息账本；控制面仍可用 step 上限止损。
                logger.debug("usage observer failed", exc_info=True)
