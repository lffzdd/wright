"""Errors raised by session commands and the session worker."""

from __future__ import annotations


class SessionServiceError(RuntimeError):
    """A UI-neutral error caused by a session operation."""


class SessionClosedError(SessionServiceError):
    pass
