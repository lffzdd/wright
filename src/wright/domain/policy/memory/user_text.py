"""Shared normalization for real user text.

Episode admission and semantic extraction both use this so a greeting is
recognized the same way. The function does not decide whether a turn is worth
storing; callers apply their own gates.
"""

from __future__ import annotations

_TRIVIAL_USER_TEXTS = frozenset({
    "hi",
    "hello",
    "hey",
    "ok",
    "okay",
    "thanks",
    "thank you",
    "thx",
    "ty",
    "你好",
    "您好",
    "嗨",
    "在吗",
    "谢谢",
    "多谢",
    "好的",
    "嗯",
    "哦",
})
_EDGE_PUNCTUATION = " \t\r\n!！.。,，?？~～"


def normalize_user_text(text: str) -> str:
    """Fold case and strip surrounding whitespace and light punctuation."""
    return text.casefold().strip().strip(_EDGE_PUNCTUATION).strip()


def is_blank_user_text(text: str) -> bool:
    """True when the text has no characters left after normalization."""
    return not isinstance(text, str) or not normalize_user_text(text)


def is_trivial_user_text(text: str) -> bool:
    """True for blank text and the shared greeting/acknowledgement list."""
    if not isinstance(text, str):
        return True
    normalized = normalize_user_text(text)
    return not normalized or normalized in _TRIVIAL_USER_TEXTS


def is_substantive_user_text(text: str) -> bool:
    """True when the text is present and not a pure greeting."""
    return isinstance(text, str) and not is_trivial_user_text(text)


__all__ = [
    "is_blank_user_text",
    "is_substantive_user_text",
    "is_trivial_user_text",
    "normalize_user_text",
]
