"""Selector port: choose candidate ids. It does not open a store."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SelectorChoice:
    """Ids proposed by one selector response.

    ``failed`` means the response as a whole is unusable. Episode injection
    must then stay empty. ``episodes_usable`` is false when the episode field
    itself has the wrong shape, even if semantic ids parsed.
    """

    memory_ids: tuple[str, ...] = ()
    episode_ids: tuple[str, ...] = ()
    failed: bool = False
    episodes_usable: bool = True
    failure_type: str = ""


class IContextSelector(ABC):
    """One shared selection request over semantic and episode candidates."""

    @abstractmethod
    def select(
        self,
        *,
        task: str,
        semantic_manifest: str,
        episode_manifest: str,
    ) -> SelectorChoice:
        """Return candidate ids. Invalid responses set ``failed``."""
        ...


__all__ = ["IContextSelector", "SelectorChoice"]
