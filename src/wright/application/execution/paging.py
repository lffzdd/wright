"""Opaque list cursors. They are not filesystem paths."""

from __future__ import annotations

import base64
import json


def encode_cursor(created_at: float, row_id: str) -> str:
    raw = json.dumps({"t": created_at, "id": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode(), validate=True))
        created_at = payload["t"]
        row_id = payload["id"]
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            raise TypeError
        if not isinstance(row_id, str) or not row_id:
            raise TypeError
        return float(created_at), row_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def bounded_page_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return limit


__all__ = ["bounded_page_limit", "decode_cursor", "encode_cursor"]
