"""Interface language lookup. Model prompts do not use this package."""

from __future__ import annotations

from collections.abc import Mapping

from .catalog import EN, lookup
from .locale import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    activate_saved_locale,
    get_locale,
    language_label,
    normalize_locale,
    set_locale,
)


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def t(key: str, /, **params: object) -> str:
    """Return interface copy for the active locale. Missing keys use English."""
    template = lookup(get_locale(), key)
    if template is None:
        return key
    if not params:
        return template
    values = {name: "" if value is None else str(value) for name, value in params.items()}
    return template.format_map(_Safe(values))


def present(code: str, params: Mapping[str, object] | None = None, *, fallback: str = "") -> str:
    """Localize a stable display code. Unknown codes keep the supplied fallback."""
    if code and code in EN:
        return t(code, **dict(params or {}))
    return fallback


def has_key(key: str) -> bool:
    return key in EN


def format_issues(issues: object) -> str:
    """Localize structured verification issues. The English message is the fallback."""
    parts: list[str] = []
    for issue in issues or ():
        if isinstance(issue, dict):
            code = str(issue.get("code") or "")
            message = str(issue.get("message") or "")
            raw_params = issue.get("params")
        else:
            code = str(getattr(issue, "code", "") or "")
            message = str(getattr(issue, "message", "") or "")
            raw_params = getattr(issue, "params", None)
        params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
        key = code if code.startswith("verification.") else f"verification.{code}"
        parts.append(present(key, params, fallback=message or t("verification.unspecified")))
    if not parts:
        return t("verification.unspecified")
    return t("verification.joiner").join(parts)


__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "activate_saved_locale",
    "format_issues",
    "get_locale",
    "has_key",
    "language_label",
    "normalize_locale",
    "present",
    "set_locale",
    "t",
]
