"""Tool executor port definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

from ..model.tool import ToolCall, ToolExecutionOutcome, ToolResult


class IToolExecutor(ABC):
    """Port for executing tool calls and prepared invocations."""

    @abstractmethod
    def execute_batch(
        self,
        indexed_invocations: Sequence[tuple[int, Any]],
        max_workers: int = 8,
        on_result: Callable[[ToolCall, ToolResult], None] | None = None,
        on_phase: Callable[[ToolCall, str], None] | None = None,
    ) -> dict[int, ToolExecutionOutcome]:
        """Execute a batch of prepared invocations concurrently and return outcomes by index."""
        ...


__all__ = ["IToolExecutor"]
