"""Port for the egress check the tool dispatcher runs before a call leaves the machine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ...model.memory import EgressVerdict


class IEgressGuard(Protocol):
    def inspect_call(
        self,
        *,
        tool_name: str,
        operations: Sequence[str],
        arguments: Mapping[str, Any],
        write_paths: Sequence[str],
        project_root: str,
        session: Any,
    ) -> EgressVerdict:
        """Return allow / ask / reject. Must not raise; an internal failure is "ask"."""
        ...


__all__ = ["IEgressGuard"]
