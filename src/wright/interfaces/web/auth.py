"""One-time bootstrap exchange for a localhost-only browser session."""

from __future__ import annotations

import hmac
import secrets
import threading


class BootstrapAuth:
    def __init__(self, bootstrap_token: str | None = None) -> None:
        self.bootstrap_token = bootstrap_token or secrets.token_urlsafe(32)
        self._sessions: set[str] = set()
        self._used = False
        self._lock = threading.Lock()

    def exchange(self, candidate: str) -> str | None:
        with self._lock:
            if self._used or not hmac.compare_digest(candidate, self.bootstrap_token):
                return None
            self._used = True
            token = secrets.token_urlsafe(32)
            self._sessions.add(token)
            return token

    def valid(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        with self._lock:
            return any(hmac.compare_digest(candidate, value) for value in self._sessions)
