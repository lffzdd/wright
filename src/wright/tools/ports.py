"""Narrow runtime ports implemented by application capability owners."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class LoopRecord(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


class LoopOperations(Protocol):
    """The small loop-management surface exposed to a concrete tool."""

    def create(
        self,
        *,
        prompt: str,
        interval_seconds: float,
        name: str = "",
    ) -> LoopRecord: ...

    def list_loops(self) -> Sequence[LoopRecord]: ...

    def stop(self, loop_id: str) -> LoopRecord: ...
