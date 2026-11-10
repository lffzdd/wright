"""Name a session after its first exchange.

A session starts with no label, so lists fall back to the first request, which
is often long or vague ("看一下这个"). After the first turn finishes, one side
query asks the model for a short title. A label the person set always wins:
the title is written only if the session is still unlabeled at write time.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ...core.logger import get_logger
from ...domain.model.session.session import INTERACTIVE_SESSION_LABEL
from ..memory.llm_util import run_side_query

if TYPE_CHECKING:
    from ...domain.gateway.llm_gateway import ILLMProvider

logger = get_logger(__name__)

TITLE_MAX_CHARS = 48
_EXCERPT_CHARS = 1_500

_SYSTEM = (
    "You name coding-assistant conversations. Read the user's first request "
    "and the assistant's reply, then return JSON {\"title\": \"...\"}. "
    "The title is a short noun phrase naming the task, at most 6 English words "
    "or 16 Chinese characters, written in the same language as the user's "
    "request. No quotes, no trailing punctuation, no emoji, no prefix such as "
    "'Title:'."
)


def is_unlabeled(session: Any) -> bool:
    label = str(getattr(session, "session_label", "") or "").strip()
    return not label or label == INTERACTIVE_SESSION_LABEL


def first_user_request(session: Any) -> str:
    """The goal of the first person-started run, or empty."""
    for run in getattr(session, "runs", {}).values():
        if getattr(run, "source", "") == "user_input" and getattr(run, "goal", ""):
            return str(run.goal)
    return ""


def clean_title(raw: str) -> str:
    """Pull a usable one-line title out of a model reply, or return empty."""
    text = raw.strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        text = str(parsed.get("title") or "")
    elif parsed is not None:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(title|标题)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    previous = None
    while previous != text:
        previous = text
        text = text.strip(" \"'`“”‘’「」《》").rstrip("。.!！?？,，;；:：")
    if len(text) > TITLE_MAX_CHARS:
        text = text[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


def generate_title(llm: ILLMProvider, request: str, reply: str) -> str:
    """Ask for a title once. Any failure yields empty; callers keep the fallback."""
    if not request.strip():
        return ""
    user = f"User request:\n{request[:_EXCERPT_CHARS]}\n\nAssistant reply:\n{reply[:_EXCERPT_CHARS]}"
    result = run_side_query(llm, _SYSTEM, user)
    if result.failed:
        logger.debug("session title query failed: %s", result.failure_type)
        return ""
    return clean_title(result.content)


__all__ = ["TITLE_MAX_CHARS", "clean_title", "first_user_request", "generate_title", "is_unlabeled"]
