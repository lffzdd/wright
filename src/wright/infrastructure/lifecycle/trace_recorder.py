"""Append-only JSONL trace for one root session."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ...application.lifecycle.contracts import LifecycleEvent


class TraceRecorder:
    """Thread-safe append-only JSONL recorder, one file per root session."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, event: LifecycleEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            needs_separator = False
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as existing:
                    existing.seek(-1, 2)
                    needs_separator = existing.read(1) != b"\n"
            with self.path.open("a", encoding="utf-8") as handle:
                if needs_separator:
                    # Preserve a crash-truncated fragment as an invalid row; do
                    # not concatenate the next valid event onto it.
                    handle.write("\n")
                handle.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        # A process may die between write() and newline flush.
                        # Later valid records must remain readable.
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        return rows

    def last_sequence(self) -> int:
        rows = self.read()
        if not rows:
            return 0
        value = rows[-1].get("sequence", 0)
        return value if isinstance(value, int) and value >= 0 else 0

