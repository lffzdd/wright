"""Semantic memory policy (Pure business rules, zero I/O).

Covers:
1. Content guardrails: reject empty text and credential-shaped content.
2. Extract gate: decide whether a finished turn is worth one extraction call.
3. Provenance review: accept an automatic memory only when its cited sources
   were actually supplied and can justify the memory type.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...model.memory.semantic import (
    SEMANTIC_READ_SCOPES,
    SEMANTIC_SCOPES,
    SEMANTIC_STATUSES,
)
from .user_text import is_blank_user_text, is_trivial_user_text

_SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sk-[A-Za-z0-9_-]{20,}", "OpenAI/API key"),
    (r"ghp_[A-Za-z0-9]{36,}", "GitHub Personal Access Token"),
    (r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----", "Private key"),
    (r"(?:postgres|mysql|mongodb)(?:\+srv)?:\/\/[^:]+:([^@]+)@", "Database connection password"),
    (r"(?:api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-.~+/=]{16,}['\"]", "API Secret Token"),
    (r"password\s*[:=]\s*['\"][^'\"]{6,}['\"]", "Plaintext password"),
)


def bounded_safe_text(value: object, limit: int) -> str:
    """Keep a short single line, or return empty when it looks like a secret.

    Empty means unknown or withheld. Callers must not replace it with a guess.
    """
    if not isinstance(value, str) or isinstance(value, bool) or limit <= 0:
        return ""
    flat = " ".join(value.split())
    if not flat:
        return ""
    if len(flat) > limit:
        flat = flat[:limit]
    safe, _reason = is_safe_memory(flat)
    if not safe:
        return ""
    return flat


def is_safe_memory(content: str) -> tuple[bool, str | None]:
    """Return whether semantic memory content is free of credential patterns."""
    for pattern, name in _SENSITIVE_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return False, f"Sensitive credential pattern detected ({name})"
    return True, None


@dataclass(frozen=True)
class SemanticMemoryPolicy:
    """Rules for accepting one semantic memory's text and sizing its recall."""

    max_candidates: int = 200
    selector_input_token_budget: int = 2048
    max_semantic_tokens_budget: int = 1200
    max_selected: int = 5

    def validate(self, content: str) -> tuple[bool, str | None]:
        if not content.strip():
            return False, "Semantic memory content cannot be empty"
        return is_safe_memory(content)


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
class SemanticExtractPolicy:
    """Decide whether a finished turn is worth a semantic-extraction model call.

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


def record_in_read_scope(
    *,
    scope: str,
    project_id: str,
    status: str,
    read_scope: str,
    current_project_id: str,
    include_inactive: bool = False,
) -> bool:
    """Whether one record may enter this read. Scope and status are independent.

    Inactive records stay out of automatic recall unless the caller asks to
    include them. An empty current project sees global memories only.
    """
    if read_scope not in SEMANTIC_READ_SCOPES or status not in SEMANTIC_STATUSES:
        return False
    if scope not in SEMANTIC_SCOPES:
        return False
    if status != "active" and not include_inactive:
        return False
    if read_scope == "global":
        return scope == "global"
    if read_scope == "current_project":
        return (
            scope == "project"
            and bool(current_project_id)
            and project_id == current_project_id
        )
    if read_scope == "all_projects":
        return scope == "project" and bool(project_id)
    if scope == "global":
        return True
    return (
        scope == "project"
        and bool(current_project_id)
        and project_id == current_project_id
    )


def scope_denial_message(
    *,
    scope: str,
    status: str,
    read_scope: str,
) -> str:
    """Explain a hidden record without claiming the file was deleted."""
    if status == "inactive":
        return (
            "This memory is inactive. Default search and automatic recall omit it. "
            "The file is still there. It can be read directly inside its own scope; "
            "restoring it takes an explicit action."
        )
    if read_scope == "applicable":
        return (
            "This memory is neither global nor in the current project. "
            "Cross-project reads or edits need an explicit scope."
        )
    return "This memory is outside this operation's scope."


def normalize_stored_scope(scope: str | None, project_id: str) -> tuple[str, str] | None:
    """Return a stored scope, or None when the file is not a current memory."""
    if scope == "global":
        return "global", ""
    if scope == "project" and project_id:
        return "project", project_id
    return None


def review_provenance(
    *,
    memory_type: str,
    source_refs: Sequence[str],
    kinds: Mapping[str, str],
) -> ProvenanceReview:
    """Accept a traceable citation. Reject invented ids and assistant-only claims.

    User preferences need a user statement. Project notes may cite a tool
    observation. Assistant-only citations are not stored as facts.
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
    if memory_type in {"user", "feedback"} and not has_user:
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
    "ExtractSignal",
    "ProvenanceReview",
    "SemanticExtractPolicy",
    "SemanticMemoryPolicy",
    "bounded_safe_text",
    "is_safe_memory",
    "normalize_stored_scope",
    "record_in_read_scope",
    "review_provenance",
    "scope_denial_message",
]
