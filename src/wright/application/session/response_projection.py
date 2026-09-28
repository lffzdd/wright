"""In-process snapshot of the response currently streaming for one session.

The web console reads this snapshot. It is not a process handle and it is
not written into the session checkpoint.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any


class ResponseProjection:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._response: dict[str, Any] | None = None

    def begin_response(self, run_id: str) -> None:
        with self._lock:
            if self._response is None or self._response["run_id"] != run_id:
                self._response = {
                    "run_id": run_id,
                    "reasoning": "",
                    "content": "",
                    "tools": {},
                }

    def append_reasoning(self, piece: str) -> None:
        with self._lock:
            if self._response is not None:
                self._response["reasoning"] += piece

    def append_content(self, piece: str) -> None:
        with self._lock:
            if self._response is not None:
                self._response["content"] += piece

    def set_content(self, content: str) -> None:
        with self._lock:
            if self._response is not None:
                self._response["content"] = content

    def update_tool(self, call_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            if self._response is None:
                return
            tool = self._response["tools"].setdefault(call_id, {})
            tool.update(deepcopy(values))

    def append_tool_output(self, call_id: str, output: str) -> None:
        with self._lock:
            if self._response is None:
                return
            tool = self._response["tools"].setdefault(call_id, {})
            tool["output"] = str(tool.get("output", "")) + output

    def response_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            if self._response is None or self._response["run_id"] != run_id:
                return None
            copied = deepcopy(self._response)
        copied["tools"] = list(copied["tools"].values())
        return copied

    def finish_response(self, run_id: str) -> None:
        with self._lock:
            if self._response is not None and self._response["run_id"] == run_id:
                self._response = None

    def clear(self) -> None:
        with self._lock:
            self._response = None
