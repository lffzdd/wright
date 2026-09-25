"""Cancellation state shared by an Agent and its execution collaborators."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager


class CancellationToken:
    """A small, composable cancellation boundary.

    The host supplies a stable check for the runtime-wide cancellation source;
    an Agent temporarily binds the check for the active run.  Consumers only
    depend on ``is_cancelled`` and never need to know which source owns the
    underlying event or control-plane record.
    """

    def __init__(self, check: Callable[[], bool] | None = None) -> None:
        self._check = check
        self._active_check: Callable[[], bool] | None = None

    def is_cancelled(self) -> bool:
        return bool(
            (self._check and self._check())
            or (self._active_check and self._active_check())
        )

    @contextmanager
    def bind_run(self, check: Callable[[], bool] | None) -> Iterator[None]:
        previous = self._active_check
        self._active_check = check
        try:
            yield
        finally:
            self._active_check = previous
