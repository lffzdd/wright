"""Stable system text shared by the model path and the interface.

``message`` is English and may be shown to a model. ``code`` is what the
interface localizes. Parameters are facts, not translated fragments.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SystemText:
    code: str
    message: str
    params: tuple[tuple[str, str], ...] = ()

    def __str__(self) -> str:
        return self.message

    def param_dict(self) -> dict[str, str]:
        return dict(self.params)


def system_text(code: str, message: str, **params: object) -> SystemText:
    pairs = tuple((key, "" if value is None else str(value)) for key, value in params.items())
    return SystemText(code, message, pairs)


__all__ = ["SystemText", "system_text"]
