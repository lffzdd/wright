"""Guardrail rules for commands and resource operations.

Permission resolution lives in :mod:`wright.domain.policy.permission`. This
module does not load or save settings files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COMMAND_DENY_PATTERNS: tuple[str, ...] = (
    r"rm\s+-rf\s+(?:/|~|\$HOME)",
    r"mkfs(?:\.|\s)",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r">\s*/dev/sd[a-z]",
    r"dd\s+if=.*of=/dev/",
    r"chmod\s+-R\s+777\s+/",
)


@dataclass(frozen=True)
class GuardrailPolicy:
    """Security guardrails: command interception, read-only mode, and path boundaries."""

    read_only: bool = False
    deny_patterns: tuple[str, ...] = COMMAND_DENY_PATTERNS
    blocked_commands: tuple[str, ...] = ("shutdown", "reboot", "init 0")

    def is_command_allowed(self, command: str) -> tuple[bool, str | None]:
        """Check whether a shell command is allowed by guardrail rules."""
        trimmed = command.strip()
        for blocked in self.blocked_commands:
            if trimmed == blocked or trimmed.startswith(f"{blocked} "):
                return False, f"Command '{blocked}' is permanently blocked by guardrail policy"
        for pattern in self.deny_patterns:
            if re.search(pattern, trimmed):
                return False, f"Command matches dangerous pattern '{pattern}'"
        return True, None

    def check_operation(self, operation: str) -> tuple[bool, str | None]:
        """Check whether an operation is allowed under current guardrail settings."""
        if self.read_only and operation in ("write", "execute", "delete", "file_write", "shell_execute"):
            return False, f"Operation '{operation}' is forbidden in read-only mode"
        return True, None


__all__ = ["COMMAND_DENY_PATTERNS", "GuardrailPolicy"]
