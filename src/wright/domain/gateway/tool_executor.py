"""Tool executor port definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any


class ToolExecutorPort(ABC):
    """Port for executing tool calls produced by the agent."""

    @abstractmethod
    def execute(self, calls: Sequence[Any]) -> Sequence[Any]:
        """Execute a batch of tool calls and return outcomes."""
        ...


# Interface compatibility aliases
IToolExecutor = ToolExecutorPort
IToolGateway = ToolExecutorPort

__all__ = ["IToolExecutor", "IToolGateway", "ToolExecutorPort"]
