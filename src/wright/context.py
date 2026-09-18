"""Build immutable request context from durable conversation records.

Session records are facts. A ``ContextView`` is a disposable, deep-copied
projection of those facts for exactly one model request. Compression therefore
cannot rewrite history, checkpoint data, UI transcript, or verification input.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .renderer import Renderer
from .session import MessageRecord
from .util import estimate_message_tokens, estimate_tools_tokens


class ContextBudgetExceeded(ValueError):
    """The deterministic projection cannot safely fit the request budget."""


@dataclass(frozen=True)
class ContextEntry:
    """One request message and its stable durable-record reference."""

    record_id: str | None
    message: dict[str, Any]
    source: str


@dataclass(frozen=True)
class ContextView:
    """A request-scoped projection with no mutable aliases into Session."""

    entries: tuple[ContextEntry, ...]
    tools: tuple[dict[str, Any], ...]
    estimated_tokens: int
    history_tokens: int
    transient_tokens: int
    tool_schema_tokens: int
    output_reserve_tokens: int
    folded_record_ids: tuple[str, ...] = ()
    over_budget: bool = False

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Return a fresh copy for adapters which may normalize wire fields."""
        return [deepcopy(entry.message) for entry in self.entries]


class ContextCompactor:
    """Deterministically fold old tool results in a ContextView only."""

    def __init__(
        self,
        renderer: Renderer,
        context_watermark: float = 0.75,
        keep_recent_tool_results: int = 3,
    ) -> None:
        self.renderer = renderer
        self.context_watermark = context_watermark
        self.keep_recent_tool_results = keep_recent_tool_results

    def project(
        self,
        entries: Sequence[ContextEntry],
        *,
        estimated_tokens: int,
        context_limit: int | None,
    ) -> tuple[tuple[ContextEntry, ...], tuple[str, ...]]:
        """Return folded copies when the watermark is exceeded.

        Tool result messages remain in place with the same call id. This keeps
        native function-call/result ordering valid for both provider adapters.
        """
        copied = [
            ContextEntry(entry.record_id, deepcopy(entry.message), entry.source)
            for entry in entries
        ]
        if (
            context_limit is None
            or estimated_tokens <= context_limit * self.context_watermark
        ):
            return tuple(copied), ()

        candidates = [
            index
            for index, entry in enumerate(copied)
            if self._is_tool_result_message(entry.message)
        ]
        keep_recent = max(0, self.keep_recent_tool_results)
        fold_indexes = candidates[:-keep_recent] if keep_recent else candidates
        folded: list[str] = []
        for index in fold_indexes:
            entry = copied[index]
            content = entry.message.get("content")
            if not isinstance(content, str):
                continue
            try:
                raw = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict) or raw.get("folded"):
                continue
            replacement = json.dumps(
                {
                    "ok": raw.get("ok", True),
                    "err": raw.get("err", ""),
                    "data": "[older tool result folded for this request]",
                    "folded": True,
                },
                ensure_ascii=False,
            )
            if len(replacement) >= len(content):
                continue
            entry.message["content"] = replacement
            if entry.record_id is not None:
                folded.append(entry.record_id)

        self.renderer.on_context_compact(
            len(folded), estimated_tokens, context_limit, self.context_watermark
        )
        return tuple(copied), tuple(folded)

    @staticmethod
    def _is_tool_result_message(message: dict[str, Any]) -> bool:
        return (
            message.get("role") == "tool"
            and isinstance(message.get("tool_call_id"), str)
            and bool(message["tool_call_id"])
            and isinstance(message.get("content"), str)
        )


class ContextBuilder:
    """Build the one-way durable-record -> request-context projection."""

    def __init__(self, compactor: ContextCompactor) -> None:
        self.compactor = compactor

    def build(
        self,
        records: Sequence[MessageRecord],
        *,
        tools: Sequence[dict[str, Any]],
        reminders: Sequence[dict[str, Any]] = (),
        context_limit: int | None = None,
        output_reserve_tokens: int | None = None,
    ) -> ContextView:
        entries = tuple(
            ContextEntry(record.id, deepcopy(record.message), record.source)
            for record in records
        ) + tuple(
            ContextEntry(None, deepcopy(message), "transient")
            for message in reminders
        )
        tool_copies = tuple(deepcopy(tool) for tool in tools)
        history_tokens = sum(
            estimate_message_tokens(entry.message)
            for entry in entries
            if entry.record_id is not None
        )
        transient_tokens = sum(
            estimate_message_tokens(entry.message)
            for entry in entries
            if entry.record_id is None
        )
        tool_tokens = estimate_tools_tokens(tool_copies)
        reserve = output_reserve_tokens if output_reserve_tokens is not None else (
            max(256, context_limit // 8) if context_limit is not None else 0
        )
        estimated = history_tokens + transient_tokens + tool_tokens + reserve
        projected, folded_ids = self.compactor.project(
            entries, estimated_tokens=estimated, context_limit=context_limit
        )
        projected_history = sum(
            estimate_message_tokens(entry.message)
            for entry in projected
            if entry.record_id is not None
        )
        projected_transient = sum(
            estimate_message_tokens(entry.message)
            for entry in projected
            if entry.record_id is None
        )
        projected_total = projected_history + projected_transient + tool_tokens + reserve
        return ContextView(
            entries=projected,
            tools=tool_copies,
            estimated_tokens=projected_total,
            history_tokens=projected_history,
            transient_tokens=projected_transient,
            tool_schema_tokens=tool_tokens,
            output_reserve_tokens=reserve,
            folded_record_ids=folded_ids,
            over_budget=context_limit is not None and projected_total > context_limit,
        )
