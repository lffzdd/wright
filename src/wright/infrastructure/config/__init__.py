"""User and package configuration stored outside the domain model."""

from __future__ import annotations

from .permission_store import (
    append_allow_rule,
    default_settings_path,
    load_permission_settings,
)

__all__ = [
    "append_allow_rule",
    "default_settings_path",
    "load_permission_settings",
]
