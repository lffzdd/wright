"""How settings layers combine, and what each layer may set.

Precedence, lowest to highest: built-in default, user file, project file,
environment. Command-line flags are applied by the caller on top.

- A project file may set only ``project_safe`` keys; others are ignored with
  a warning, as a project ``.env`` is today.
- No file may hold a secret. Keys that look like credentials are rejected.
- An invalid value is ignored with a warning and the lower layer stands,
  except a ``fail_closed`` key in a trusted layer (user file or
  environment), which is an error: falling back to the default endpoint or
  path could send data somewhere the user did not choose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..model.settings import (
    SETTING_SPECS,
    SPECS_BY_KEY,
    SettingScope,
    SettingsError,
    SettingSource,
    WrightSettings,
)

_SECRET_MARKERS = ("api_key", "apikey", "token", "secret", "password")


@dataclass(frozen=True)
class ResolvedSettings:
    settings: WrightSettings
    warnings: tuple[str, ...] = ()


def looks_secret(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in _SECRET_MARKERS)


def flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Turn ``{"llm": {"model": "x"}}`` into ``{"llm.model": "x"}``."""
    flat: dict[str, Any] = {}
    for name, value in data.items():
        key = f"{prefix}{name}"
        if isinstance(value, Mapping):
            flat.update(flatten(value, f"{key}."))
        else:
            flat[key] = value
    return flat


def environment_layer(environ: Mapping[str, str]) -> dict[str, str]:
    """Pick each key's first non-empty legacy environment variable."""
    layer: dict[str, str] = {}
    for spec in SETTING_SPECS:
        for name in spec.env:
            raw = environ.get(name)
            if raw is not None and raw.strip():
                layer[spec.key] = raw
                break
    return layer


def resolve_settings(
    *,
    user: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    user_label: str = "user settings",
    project_label: str = "project settings",
) -> ResolvedSettings:
    """Merge the layers. ``user`` and ``project`` are nested file contents."""
    values: dict[str, Any] = {spec.key: spec.default for spec in SETTING_SPECS}
    sources: dict[str, SettingSource] = dict.fromkeys(values, "default")
    warnings: list[str] = []
    layers: tuple[tuple[SettingSource, Mapping[str, Any], str], ...] = (
        ("user", flatten(user or {}), user_label),
        ("project", flatten(project or {}), project_label),
        ("env", environment_layer(environ or {}), "environment"),
    )
    for source, layer, label in layers:
        for key, raw in layer.items():
            problem = _layer_problem(source, key)
            if problem is not None:
                warnings.append(f"Ignored {key} in {label}: {problem}")
                continue
            spec = SPECS_BY_KEY[key]
            try:
                values[key] = spec.coerce(raw)
            except SettingsError as exc:
                if spec.fail_closed and source != "project":
                    raise SettingsError(f"Invalid {key} in {label}: {exc}") from exc
                warnings.append(f"Ignored {key} in {label}: {exc}")
                continue
            sources[key] = source
    return ResolvedSettings(
        settings=WrightSettings.from_values(values, sources),
        warnings=tuple(warnings),
    )


def _layer_problem(source: SettingSource, key: str) -> str | None:
    if looks_secret(key):
        return "secrets belong in the environment or .env, not a settings file"
    spec = SPECS_BY_KEY.get(key)
    if spec is None:
        return "unknown setting"
    if source == "project" and not spec.project_safe:
        return "only the user settings file may set it"
    return None


def validate_write(scope: SettingScope, key: str, value: object) -> Any:
    """Check one edit before it is written. ``None`` removes the key."""
    problem = _layer_problem(scope, key)
    if problem is not None:
        raise SettingsError(f"Cannot set {key} in {scope} settings: {problem}")
    if value is None:
        return None
    spec = SPECS_BY_KEY[key]
    return spec.to_json(spec.coerce(value))


__all__ = [
    "ResolvedSettings",
    "environment_layer",
    "flatten",
    "looks_secret",
    "resolve_settings",
    "validate_write",
]
