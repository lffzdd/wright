"""Bootstrap authentication for localhost-only browser sessions."""

from __future__ import annotations

import hmac
import secrets
import threading

COOKIE_NAME = "wright_web_session"


class BootstrapAuth:
    def __init__(self, bootstrap_token: str | None = None) -> None:
        self.bootstrap_token = bootstrap_token or secrets.token_urlsafe(32)
        self._sessions: set[str] = set()
        self._lock = threading.Lock()

    def exchange(self, candidate: str) -> str | None:
        with self._lock:
            if not hmac.compare_digest(candidate, self.bootstrap_token):
                return None
            token = secrets.token_urlsafe(32)
            self._sessions.add(token)
            return token

    def valid(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        with self._lock:
            return any(hmac.compare_digest(candidate, value) for value in self._sessions)

