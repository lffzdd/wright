"""Extraction gate and provenance review for automatic fact extraction.

The gate is pure rules and never calls a model. A positive decision only means
"worth one extraction call"; the model may still return nothing. Provenance
review accepts an extracted fact only when its citations were actually
supplied and can justify the fact kind.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .user_text import is_blank_user_text, is_trivial_user_text

# Phrases that suggest a durable preference, correction, or constraint.
# They only open the extraction call. They do not prove a memory should be saved.
# Implicit preferences and novel wording are expected misses.
_DURABLE_USER = re.compile(
    r"记住|记一下|以后(?:都|不要|别|必须|只用|不再)|别再|不要再|"
    r"我习惯|我更喜欢|我偏好|我们决定|必须用|不要用|改成用|"
    r"\bremember that\b|\bfrom now on\b|\bi prefer\b|\bi'd rather\b|"
    r"\bwe decided\b|\bnever again\b|\bdo not use\b|\bdon't use\b|\bthe rule is\b",
    re.IGNORECASE,
)
_EPHEMERAL_COMMAND = re.compile(
    r"^\s*(?:pwd|ls|dir|whoami|date|echo|which|uname|hostname|tree)\b",
    re.IGNORECASE,
)
_WEATHER = re.compile(r"(天气|weather|wttr)", re.IGNORECASE)
_EPHEMERAL_USER = re.compile(
    r"^(?:pwd|ls|dir|当前目录|看看目录|查一下天气|查天气|天气怎么样|"
    r"what(?:'s| is) the weather|list (?:the )?(?:files|directory))$",
    re.IGNORECASE,
)
_JUSTIFYING_ORIGINS = frozenset({
    "user_statement",
    "tool_observation",
    "verification_record",
    "mixed",
})


@dataclass(frozen=True)
class ExtractSignal:
    """One real user statement or tool observation already captured for the turn.

    Assistant text is not a signal. Recall blocks, hooks, and runtime events
    must not be passed in.
    """

    evidence_id: str
    kind: str
    text: str = ""
    command: str = ""
    subject: str = ""
    execution_status: str = ""
    ok: bool | None = None
    error: str = ""


@dataclass(frozen=True)
class ExtractDecision:
    """Whether to spend one extraction call, and which source ids may be cited."""

    should_extract: bool
    reason_codes: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProvenanceReview:
    """Result of checking one automatic memory against the supplied sources.

    ``accept`` means the citation is traceable. It does not mean the text is true.
    """

    accept: bool
    reason: str = ""
    origin: str = ""
    source_refs: tuple[str, ...] = ()


def _durable_user(text: str) -> bool:
    if not isinstance(text, str) or is_trivial_user_text(text):
        return False
    return _DURABLE_USER.search(text) is not None


def _ephemeral_command(command: str) -> bool:
    text = command.strip()
    if not text:
        return False
    return _EPHEMERAL_COMMAND.match(text) is not None or _WEATHER.search(text) is not None


def _ephemeral_user(text: str) -> bool:
    if not isinstance(text, str):
        return False
    compact = " ".join(text.split())
    return _EPHEMERAL_USER.match(compact) is not None or _WEATHER.search(compact) is not None


def _tool_failed(signal: ExtractSignal) -> bool:
    if signal.error.strip():
        return True
    if signal.ok is False:
        return True
    return signal.execution_status in {"failed", "timeout"}


def _tool_succeeded(signal: ExtractSignal) -> bool:
    return signal.ok is True and not signal.error.strip()


@dataclass(frozen=True)
class ExtractPolicy:
    """Decide whether a finished turn is worth a fact-extraction model call.

    The decision is a hint to spend a call, not a judgment that durable
    knowledge exists. Tool count, a single file edit, and a lone failure are
    not enough. The model may still return an empty list.
    """

    def decide(
        self,
        signals: Sequence[ExtractSignal],
        *,
        verification: Sequence[Mapping[str, Any]] = (),
    ) -> ExtractDecision:
        users = [item for item in signals if item.kind == "user_statement"]
        tools = [item for item in signals if item.kind == "tool_observation"]
        durable = [item for item in users if _durable_user(item.text)]
        followup, followup_ids = _failure_followup(tools, verification)
        if durable or followup:
            refs = [item.evidence_id for item in durable if item.evidence_id]
            refs.extend(item for item in followup_ids if item)
            reasons: list[str] = []
            if durable:
                reasons.append("user_durable_statement")
            if followup:
                reasons.append("failure_with_followup")
            return ExtractDecision(True, tuple(reasons), tuple(dict.fromkeys(refs)))
        return ExtractDecision(False, (_skip_reason(users, tools, verification),), ())

    def should_extract(
        self,
        signals: Sequence[ExtractSignal],
        *,
        verification: Sequence[Mapping[str, Any]] = (),
    ) -> ExtractDecision:
        """Structured gate. A bool is not enough for skip reasons and source ids."""
        return self.decide(signals, verification=verification)


def _failure_followup(
    tools: Sequence[ExtractSignal],
    verification: Sequence[Mapping[str, Any]],
) -> tuple[bool, list[str]]:
    """A failure plus a later success is a signal. Failure alone is not."""
    refs: list[str] = []
    matched = False
    saw_failure = False
    failure_ids: list[str] = []
    for signal in tools:
        if _tool_failed(signal):
            saw_failure = True
            if signal.evidence_id:
                failure_ids.append(signal.evidence_id)
            continue
        if saw_failure and _tool_succeeded(signal):
            matched = True
            refs.extend(failure_ids)
            if signal.evidence_id:
                refs.append(signal.evidence_id)
            break
    saw_reject = False
    for item in verification:
        approved = item.get("approved")
        if approved is False:
            saw_reject = True
        elif saw_reject and approved is True:
            matched = True
            evidence_id = item.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id:
                refs.append(evidence_id)
            break
    return matched, refs


def _skip_reason(
    users: Sequence[ExtractSignal],
    tools: Sequence[ExtractSignal],
    verification: Sequence[Mapping[str, Any]],
) -> str:
    texts = [item.text for item in users]
    if not texts or all(is_blank_user_text(text) for text in texts):
        if not tools:
            return "blank"
    if texts and all(is_trivial_user_text(text) for text in texts) and not tools:
        return "trivial_user_text"
    if texts and all(_ephemeral_user(text) or is_trivial_user_text(text) for text in texts):
        if tools and all(_ephemeral_command(item.command) or _ephemeral_user(item.text) for item in tools):
            return "ephemeral_activity"
        if not tools and any(_ephemeral_user(text) for text in texts):
            return "ephemeral_activity"
    if any(_tool_failed(item) for item in tools) or any(
        item.get("approved") is False for item in verification
    ):
        return "failure_without_followup"
    return "no_durable_signal"


def review_provenance(
    *,
    kind: str,
    source_refs: Sequence[str],
    kinds: Mapping[str, str],
) -> ProvenanceReview:
    """Accept a traceable citation. Reject invented ids and assistant-only claims.

    Preferences and feedback need a user statement. Constraints and facts may
    cite a tool observation. Assistant-only citations are not stored as facts.
    """
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
        return ProvenanceReview(False, "missing_source_refs")
    if not source_refs or not all(isinstance(item, str) and item for item in source_refs):
        return ProvenanceReview(False, "missing_source_refs")
    unknown = [item for item in source_refs if item not in kinds]
    if unknown:
        return ProvenanceReview(False, "unknown_source_ref")
    cited = tuple(dict.fromkeys(source_refs))
    cited_kinds = {kinds[item] for item in cited}
    if cited_kinds <= {"assistant_statement"}:
        return ProvenanceReview(False, "assistant_only")
    has_user = "user_statement" in cited_kinds
    has_tool = "tool_observation" in cited_kinds
    has_verification = "verification_record" in cited_kinds
    if kind in {"preference", "feedback"} and not has_user:
        return ProvenanceReview(False, "preference_without_user_statement")
    if not has_user and not has_tool and not has_verification:
        return ProvenanceReview(False, "unsupported_origin")
    if has_user and (has_tool or has_verification):
        origin = "mixed"
    elif has_user:
        origin = "user_statement"
    elif has_tool and has_verification:
        origin = "mixed"
    elif has_tool:
        origin = "tool_observation"
    else:
        origin = "verification_record"
    if origin not in _JUSTIFYING_ORIGINS:
        return ProvenanceReview(False, "unsupported_origin")
    return ProvenanceReview(True, "", origin, cited)


__all__ = [
    "ExtractDecision",
    "ExtractPolicy",
    "ExtractSignal",
    "ProvenanceReview",
    "review_provenance",
]
