"""Public tool fields shared by live and persisted session projections."""

from __future__ import annotations

from typing import Any

_RESULT_FIELDS = frozenset({
    "ok", "err", "data", "summary", "content", "artifacts", "retention", "retention_key",
})


def tool_phase(status: Any) -> str:
    """Translate domain statuses and clamp unknown values to a failed UI phase."""
    raw = str(status)
    phase = {"pending": "planned", "timeout": "failed"}.get(raw, raw)
    return phase if phase in {"planned", "awaiting_approval", "running", "succeeded", "failed"} else "failed"


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
