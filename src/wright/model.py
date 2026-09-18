"""Provider-neutral request contract between ContextBuilder and model adapters."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelRequest(Sequence[dict[str, Any]]):
    """A single immutable model request built from a ContextView.

    It implements ``Sequence`` temporarily so existing fake model callables can
    inspect messages while production gateways consume the explicit contract.
    """

    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    response_format: dict[str, Any] | None = None
    model: str | None = None
    transport: str | None = None
    context_token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.messages[index]

    def copied_messages(self) -> list[dict[str, Any]]:
        return deepcopy(list(self.messages))
