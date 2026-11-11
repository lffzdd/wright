"""Public tool fields shared by live and persisted session projections."""

from __future__ import annotations

from typing import Any

_RESULT_FIELDS = frozenset({
    "ok", "err", "data", "summary", "content", "artifacts", "retention", "retention_key",
    "display",
})


_UI_PHASES = frozenset(
    {"planned", "awaiting_approval", "running", "succeeded", "failed"}
)
_DOMAIN_STATUS_PHASE = {
    "pending": "planned",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "timeout": "failed",
}


def tool_phase(status: Any) -> str:
    """Map durable execution statuses to UI phases without masking new domain values."""
    if status is None:
        return "failed"
    raw = str(status)
    if not raw:
        return "failed"
    if raw in _UI_PHASES:
        return raw
    mapped = _DOMAIN_STATUS_PHASE.get(raw)
    if mapped is not None:
        return mapped
    return raw


def public_tool_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep the ToolResult envelope, excluding event metadata and storage paths."""
    if result is None:
        return None
    cleaned = {key: value for key, value in result.items() if key in _RESULT_FIELDS}
    if isinstance(cleaned.get("artifacts"), list):
        cleaned["artifacts"] = [
            {key: value for key, value in artifact.items() if key != "storage_path"}
            if isinstance(artifact, dict) else artifact
            for artifact in cleaned["artifacts"]
        ]
    return cleaned
