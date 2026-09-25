"""Fact memory policy (Pure business rules, zero I/O).

Responsible for:
1. Guardrail validation: preventing API keys, passwords, and sensitive credentials from entering fact memory.
2. Conflict resolution: Upsert rules, key-based deduplication, and scope precedence (PROJECT > GLOBAL).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..model.fact import Fact, FactScope

# Pattern rules preventing secret/credential leakage into persistent facts
_SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sk-[A-Za-z0-9_-]{20,}", "OpenAI/API key"),
    (r"ghp_[A-Za-z0-9]{36,}", "GitHub Personal Access Token"),
    (r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----", "Private key"),
    (r"(?:postgres|mysql|mongodb)(?:\+srv)?:\/\/[^:]+:([^@]+)@", "Database connection password"),
    (r"(?:api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-.~+/=]{16,}['\"]", "API Secret Token"),
    (r"password\s*[:=]\s*['\"][^'\"]{6,}['\"]", "Plaintext password"),
)


def is_safe_fact(content: str) -> tuple[bool, str | None]:
    """Check if fact content is safe to record without exposing credentials.
    
    Returns (True, None) if safe, or (False, reason) if sensitive pattern detected.
    """
    for pattern, name in _SENSITIVE_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return False, f"Sensitive credential pattern detected ({name})"
    return True, None


@dataclass(frozen=True)
class FactPolicy:
    """Pure domain rules governing fact retention, conflict resolution, and scope precedence."""

    project_overrides_global: bool = True

    def validate_fact(self, fact: Fact) -> tuple[bool, str | None]:
        """Validate if a Fact instance meets quality and safety standards."""
        if not fact.content.strip():
            return False, "Fact content cannot be empty"
        return is_safe_fact(fact.content)

    def filter_safe(self, facts: Sequence[Fact]) -> tuple[Fact, ...]:
        """Filter out any facts that fail safety checks."""
        return tuple(f for f in facts if self.validate_fact(f)[0])

    def resolve_precedence(self, existing: Fact, incoming: Fact) -> Fact:
        """Resolve conflict between two facts with the same key.
        
        Rule:
        1. If one is PROJECT and one is GLOBAL, PROJECT takes precedence.
        2. If both have the same scope, the incoming fact overwrites existing (Upsert).
        """
        if self.project_overrides_global:
            if existing.scope == "PROJECT" and incoming.scope == "GLOBAL":
                return existing
            if incoming.scope == "PROJECT" and existing.scope == "GLOBAL":
                return incoming
        # Same scope or override enabled: incoming replaces existing
        return incoming

    def deduplicate_facts(self, facts: Sequence[Fact]) -> tuple[Fact, ...]:
        """Deduplicate facts by key, applying precedence and upsert rules."""
        resolved: dict[str, Fact] = {}
        for fact in facts:
            key = fact.key or fact.id
            if key in resolved:
                resolved[key] = self.resolve_precedence(resolved[key], fact)
            else:
                resolved[key] = fact
        return tuple(resolved.values())


__all__ = [
    "FactPolicy",
    "is_safe_fact",
]
