"""Interface locale. This module does not build model prompts."""

from __future__ import annotations

import threading

from ...infrastructure.config.preferences import preference_value, save_preference

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "zh-CN")
LOCALE_LABELS = {
    "en": "English",
    "zh-CN": "简体中文",
}

_LOCK = threading.RLock()
_current = DEFAULT_LOCALE


def normalize_locale(value: object) -> str:
    """Return a supported locale. Anything else is English."""
    if isinstance(value, str) and value in SUPPORTED_LOCALES:
        return value
    return DEFAULT_LOCALE


def get_locale() -> str:
    with _LOCK:
        return _current


def set_locale(value: object, *, persist: bool = False) -> str:
    """Apply a locale for this process. Persist only when asked."""
    global _current
    locale = normalize_locale(value)
    with _LOCK:
        _current = locale
    if persist:
        save_preference("interface_language", locale)
    return locale


def activate_saved_locale() -> str:
    """Load the application preference. Invalid or missing values stay English."""
    return set_locale(preference_value("interface_language"), persist=False)


def language_label(locale: str | None = None) -> str:
    selected = normalize_locale(locale or get_locale())
    return LOCALE_LABELS[selected]


__all__ = [
    "DEFAULT_LOCALE",
    "LOCALE_LABELS",
    "SUPPORTED_LOCALES",
    "activate_saved_locale",
    "get_locale",
    "language_label",
    "normalize_locale",
    "set_locale",
]
