"""Pure serialization and validation helpers for the autonomy store."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ._base import AutonomyStoreError


def _bounded(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise AutonomyStoreError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise AutonomyStoreError(f"{name} must be non-empty")
    if len(cleaned) > limit:
        raise AutonomyStoreError(f"{name} exceeds {limit} chars")
    return cleaned

def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AutonomyStoreError(f"value is not JSON serializable: {exc}") from exc

def _load_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AutonomyStoreError(f"corrupt JSON in autonomy DB: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AutonomyStoreError("autonomy DB JSON value must be an object")
    return parsed

def _bounded_history_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Keep durable history queryable without copying binary or huge output."""
    if not isinstance(value, dict):
        return {"value": repr(value)[:8_000]}

    def trim(item: Any, depth: int = 0) -> Any:
        if depth > 5:
            return repr(item)[:2_000]
        if isinstance(item, str):
            return item if len(item) <= 12_000 else item[:11_997] + "..."
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        if isinstance(item, dict):
            return {str(key)[:200]: trim(val, depth + 1) for key, val in list(item.items())[:200]}
        if isinstance(item, (list, tuple)):
            return [trim(val, depth + 1) for val in item[:200]]
        return repr(item)[:2_000]

    result = trim(value)
    return result if isinstance(result, dict) else {"value": result}

def _hash_payload(value: dict[str, Any]) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()[:32]

def _safe_run_config(value: dict[str, Any]) -> dict[str, Any]:
    """Persist configuration snapshots, never credential values.

    The present API accepts only the small declarative durable-run surface.
    Unknown nesting is rejected so a caller cannot accidentally copy an
    environment or API key into the task database.
    """
    if not isinstance(value, dict):
        raise AutonomyStoreError("run_config must be an object")
    allowed = {"profile", "model", "transport", "environment", "max_steps"}
    unknown = set(value) - allowed
    if unknown:
        raise AutonomyStoreError(f"unsupported run_config fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if key == "max_steps":
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 1_000:
                raise AutonomyStoreError("run_config.max_steps must be between 1 and 1000")
            clean[key] = item
        elif key in {"profile", "model", "transport", "environment"}:
            clean[key] = _bounded(str(item), f"run_config.{key}", 200)
    if "profile" in clean and clean["profile"] != "durable":
        raise AutonomyStoreError("durable automations only support the durable profile")
    if "transport" in clean and clean["transport"] not in {"auto", "chat", "responses"}:
        raise AutonomyStoreError("run_config.transport must be auto, chat, or responses")
    if "environment" in clean and clean["environment"] != "local":
        raise AutonomyStoreError("durable automations currently require the local environment")
    return clean

def _redact_arguments(value: dict[str, Any]) -> dict[str, Any]:
    sensitive = {"api_key", "authorization", "token", "password", "secret"}
    return {
        str(key): "[redacted]" if str(key).casefold() in sensitive else item
        for key, item in value.items()
    }
