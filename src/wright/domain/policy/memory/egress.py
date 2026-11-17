"""Egress rules: classify where a tool call sends text, and what in it must not leave.

Pure functions. The application layer gathers facts and the tool call, calls
these, and turns the verdict into "run", "ask the user" or "refuse".

Rules (docs/memory.md, "边界规则"):
- R1  credentials never reach E1-E3, approval or not.
- R2  P1/P2 facts do not reach E1-E3 unless the user typed that content this turn.
- R3  a repo space's P0 facts do not reach another repo's artifacts or E3.
- R4  team rules files are written only when the user asked for it this turn.
- R8  anything that cannot be decided is treated as a hit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from ...model.memory import (
    EgressHit,
    EgressRequest,
    EgressVerdict,
    Fact,
    InfoLevel,
    SinkLevel,
    space_type_of,
)
from .secrets import find_secrets
from .write import normalize_for_match

# --- sink classification -------------------------------------------------

_GIT_OUTBOUND = re.compile(
    r"\bgit\b(?:\s+-[-\w]+(?:[ =][^\s]+)?)*\s+(?:commit|push|tag|notes|send-email|request-pull|am)\b"
)
_FORGE = re.compile(r"(?:^|[\s;&|(])(?:gh|glab|hub|tea)\s+[a-z]")
_PUBLISH = re.compile(
    r"\b(?:npm|pnpm|yarn|bun)\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b|"
    r"\bcargo\s+publish\b|\bgem\s+push\b|\bpoetry\s+publish\b|\buv\s+publish\b|\bhelm\s+push\b"
)
_NETWORK = re.compile(
    r"(?:^|[\s;&|(])(?:curl|wget|xh|httpie|http|https|nc|ncat|telnet|ssh|scp|sftp|rsync|ftp|"
    r"mail|mailx|sendmail|mutt|aws|gcloud|az|gsutil|s3cmd|kubectl)(?=\s|$)"
)
_SHELL_FILE_WRITE = re.compile(r"(?<![<>&\d])>{1,2}(?![&>])|\btee\b|\bsed\s+(?:-[a-zA-Z]*i|--in-place)|<<-?\s*['\"]?\w+")

SHARED_RULE_FILES = frozenset({
    "agents.md",
    "claude.md",
    "gemini.md",
    ".cursorrules",
    ".windsurfrules",
    "copilot-instructions.md",
    "contributing.md",
    "codeowners",
})
_RULES_REQUEST = re.compile(
    r"agents\.md|claude\.md|gemini\.md|cursorrules|copilot-instructions|contributing|codeowners|"
    r"团队规范|项目规范|协作规范|规范文件|规则文件|\bteam (?:rule|convention|guideline)s?\b|\bproject rules?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SinkClass:
    sink: SinkLevel
    detail: str = ""
    shared_rules_file: str = ""


def classify_command(command: str) -> SinkClass:
    text = str(command or "")
    if _GIT_OUTBOUND.search(text):
        return SinkClass("E2", "git")
    if _FORGE.search(text):
        return SinkClass("E2", "forge")
    if _PUBLISH.search(text):
        return SinkClass("E2", "publish")
    if _NETWORK.search(text):
        return SinkClass("E3", "network command")
    if _SHELL_FILE_WRITE.search(text):
        return SinkClass("E1", "shell file write")
    return SinkClass("E0")


def classify_sink(
    operations: Iterable[str],
    *,
    command: str = "",
    write_paths: Sequence[str] = (),
    project_root: str = "",
) -> SinkClass:
    """Pick the highest sink the declared operations can reach."""
    ops = set(operations)
    best = SinkClass("E0")
    if "shell" in ops:
        best = _higher(best, classify_command(command))
    if "file_write" in ops:
        shared = next((path for path in write_paths if PurePath(path).name.casefold() in SHARED_RULE_FILES), "")
        if shared:
            best = _higher(best, SinkClass("E2", "shared rules file", shared))
        elif not write_paths or not project_root or any(_inside(path, project_root) for path in write_paths):
            best = _higher(best, SinkClass("E1", "file write"))
    if ops & {"network_read", "network_write"}:
        best = _higher(best, SinkClass("E3", "network"))
    if "external_unknown" in ops:
        best = _higher(best, SinkClass("E3", "external tool"))
    return best


_RANK = {"E0": 0, "E1": 1, "E2": 2, "E3": 3}


def _higher(left: SinkClass, right: SinkClass) -> SinkClass:
    if _RANK[right.sink] > _RANK[left.sink]:
        return right
    if right.shared_rules_file and not left.shared_rules_file:
        return SinkClass(left.sink, left.detail, right.shared_rules_file)
    return left


def _inside(path: str, root: str) -> bool:
    try:
        candidate = PurePath(path)
        base = PurePath(root)
        if not candidate.is_absolute():
            return True
        return candidate == base or base in candidate.parents
    except Exception:
        return True


def outbound_text(arguments: Mapping[str, Any], *, limit: int = 400_000) -> str:
    """Every string leaf of a tool call, paths included: a file name can leak too."""
    parts: list[str] = []
    size = 0

    def walk(value: Any) -> None:
        nonlocal size
        if size >= limit:
            return
        if isinstance(value, str):
            parts.append(value)
            size += len(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(arguments)
    return "\n".join(parts)[:limit]


# --- what must not leave -------------------------------------------------

@dataclass(frozen=True)
class ProtectedItem:
    fact_id: str
    level: InfoLevel
    space_id: str
    content: str
    markers: tuple[str, ...]
    summary: str


def protected_items(facts: Iterable[Fact], *, current_space_id: str) -> list[ProtectedItem]:
    """Facts whose content may not leave from this space.

    Superseded facts still describe the user, so they stay protected.
    Quarantined facts are someone else's text and deleted facts are gone.
    """
    items: list[ProtectedItem] = []
    for fact in facts:
        if fact.status not in {"active", "superseded"} or fact.shareable:
            continue
        level: InfoLevel | None
        if fact.sensitivity == "sensitive":
            level = "sensitive"
        elif fact.sensitivity == "personal":
            level = "personal"
        elif _is_foreign_repo(fact.space_id, current_space_id):
            level = "foreign_project"
        else:
            level = None
        if level is None:
            continue
        items.append(ProtectedItem(
            fact_id=fact.id,
            level=level,
            space_id=fact.space_id,
            content=fact.content,
            markers=fact.markers,
            summary=fact.summary,
        ))
    return items


def _is_foreign_repo(space_id: str, current_space_id: str) -> bool:
    try:
        return space_type_of(space_id) == "repo" and space_id != current_space_id
    except Exception:
        return False


CONTENT_SHINGLE = 10
MIN_CONTENT_MATCH = 6


def scan(
    text: str,
    items: Sequence[ProtectedItem],
    *,
    user_texts: Sequence[str] = (),
) -> list[EgressHit]:
    """Deterministic layers: credentials, markers, then verbatim content runs.

    Text the user typed this turn does not count as a hit (R2's exception).
    """
    hits: list[EgressHit] = []
    for kind, excerpt in find_secrets(text):
        hits.append(EgressHit("secret", "credential", excerpt, summary=kind))
    if not items:
        return hits
    norm_text = normalize_for_match(text)
    if not norm_text:
        return hits
    user_joined = "\n".join(user_texts)
    norm_user = normalize_for_match(user_joined)
    text_runs = _runs(norm_text, CONTENT_SHINGLE)
    user_runs = _runs(norm_user, CONTENT_SHINGLE) if norm_user else set()
    for item in items:
        hit = _marker_hit(item, text, norm_text, user_joined) or _content_hit(item, text_runs, user_runs, norm_text, norm_user)
        if hit is not None:
            hits.append(hit)
    return hits


def _marker_hit(item: ProtectedItem, text: str, norm_text: str, user_text: str) -> EgressHit | None:
    for marker in item.markers:
        if not _marker_in(marker, text, norm_text) or (user_text and _marker_in(marker, user_text, normalize_for_match(user_text))):
            continue
        return EgressHit(item.level, "marker", marker, item.fact_id, item.space_id, item.summary)
    return None


def _marker_in(marker: str, text: str, norm_text: str) -> bool:
    """CJK markers match as normalized substrings. Latin ones match whole words,
    case-sensitively when the marker has capitals, so a person called "Ray"
    does not match ``import ray``."""
    key = normalize_for_match(marker)
    if len(key) < 2:
        return False
    if any("\u3400" <= ch <= "\u9fff" for ch in key):
        return key in norm_text
    flags = 0 if any(ch.isupper() for ch in marker) else re.IGNORECASE
    pattern = r"(?<![\w.-])" + re.escape(marker.strip()) + r"(?![\w-])"
    return re.search(pattern, text, flags) is not None


def _content_hit(
    item: ProtectedItem,
    text_runs: set[str],
    user_runs: set[str],
    norm_text: str,
    norm_user: str,
) -> EgressHit | None:
    norm = normalize_for_match(item.content)
    if len(norm) < MIN_CONTENT_MATCH:
        return None
    if len(norm) < CONTENT_SHINGLE:
        if norm in norm_text and not (norm_user and norm in norm_user):
            return EgressHit(item.level, "content", norm, item.fact_id, item.space_id, item.summary)
        return None
    for run in _runs(norm, CONTENT_SHINGLE):
        if run in text_runs and run not in user_runs:
            return EgressHit(item.level, "content", run, item.fact_id, item.space_id, item.summary)
    return None


def _runs(norm: str, size: int) -> set[str]:
    if len(norm) < size:
        return {norm} if norm else set()
    return {norm[index:index + size] for index in range(len(norm) - size + 1)}


def rules_file_requested(user_texts: Sequence[str]) -> bool:
    return any(_RULES_REQUEST.search(text or "") for text in user_texts)


def decide(request: EgressRequest, hits: Sequence[EgressHit]) -> EgressVerdict:
    """Matrix from docs/memory.md. E0 never asks; credentials are refused outright."""
    if request.sink == "E0":
        return EgressVerdict.allow()
    if any(hit.level == "secret" for hit in hits):
        return EgressVerdict("reject", tuple(hits), "credential in outbound content")
    if hits:
        return EgressVerdict("ask", tuple(hits), "outbound content may carry private memory")
    return EgressVerdict.allow()


def describe_verdict(verdict: EgressVerdict) -> str:
    """Short text for the approval prompt and the tool result."""
    lines = []
    for hit in verdict.hits[:6]:
        source = f" (memory {hit.fact_id}: {hit.summary})" if hit.fact_id else ""
        label = {
            "secret": "credential",
            "sensitive": "sensitive personal information",
            "personal": "personal information",
            "foreign_project": "information from another project",
            "shared_rules": "team rules file the user did not ask to change",
        }.get(hit.level, hit.level)
        detail = {
            "check_failed": "the egress check failed, so this call is held",
            "judge_unavailable": "the paraphrase check could not run, so this call is held",
        }.get(hit.reason, f'"{hit.snippet}"')
        lines.append(f"- {label}: {detail}{source}")
    return "\n".join(lines)


__all__ = [
    "CONTENT_SHINGLE",
    "SHARED_RULE_FILES",
    "ProtectedItem",
    "SinkClass",
    "classify_command",
    "classify_sink",
    "decide",
    "describe_verdict",
    "outbound_text",
    "protected_items",
    "rules_file_requested",
    "scan",
]
