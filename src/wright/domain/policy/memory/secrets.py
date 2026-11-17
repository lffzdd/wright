"""Credential detection (level S). Pure, shared by memory writes and the egress check.

A match means "looks like a credential". It does not prove one; false
positives are accepted because a missed secret cannot be taken back.
"""

from __future__ import annotations

import re

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, flags), name)
    for pattern, flags, name in (
        (r"sk-ant-[A-Za-z0-9_-]{20,}", 0, "Anthropic API key"),
        (r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}", 0, "OpenAI-style API key"),
        (r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}", 0, "Stripe live key"),
        (r"\bgh[pousr]_[A-Za-z0-9]{30,}", 0, "GitHub token"),
        (r"\bgithub_pat_[A-Za-z0-9_]{40,}", 0, "GitHub fine-grained token"),
        (r"\bglpat-[A-Za-z0-9_-]{20,}", 0, "GitLab token"),
        (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", 0, "Slack token"),
        (r"\bAKIA[0-9A-Z]{16}\b", 0, "AWS access key"),
        (r"\bAIza[0-9A-Za-z_-]{35}\b", 0, "Google API key"),
        (r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----", 0, "Private key"),
        (r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", 0, "JWT"),
        (r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s/]+:[^@\s]{3,}@", re.IGNORECASE,
         "Connection string with password"),
        (r"\bbearer\s+[A-Za-z0-9_\-.~+/=]{24,}", re.IGNORECASE, "Bearer token"),
        (
            (
                r"(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)"
                r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-.~+/=]{16,}"
            ),
            re.IGNORECASE,
            "API secret",
        ),
        (r"password\s*[:=]\s*['\"][^'\"\s]{6,}['\"]", re.IGNORECASE, "Plaintext password"),
    )
)


def find_secrets(text: str) -> list[tuple[str, str]]:
    """Return ``(kind, redacted excerpt)`` for each credential-shaped match."""
    if not isinstance(text, str) or not text:
        return []
    found: list[tuple[str, str]] = []
    for pattern, name in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            found.append((name, _redact(value)))
            if len(found) >= 10:
                return found
    return found


def is_safe_memory(content: str) -> tuple[bool, str | None]:
    """Whether text is free of credential patterns, with the first reason when not."""
    hits = find_secrets(content)
    if hits:
        return False, f"Sensitive credential pattern detected ({hits[0][0]})"
    return True, None


def bounded_safe_text(value: object, limit: int) -> str:
    """Keep a short single line, or return empty when it looks like a secret."""
    if not isinstance(value, str) or limit <= 0:
        return ""
    flat = " ".join(value.split())
    if not flat:
        return ""
    flat = flat[:limit]
    return "" if find_secrets(flat) else flat


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return value[:4] + "…" + "*" * 4


__all__ = ["bounded_safe_text", "find_secrets", "is_safe_memory"]
