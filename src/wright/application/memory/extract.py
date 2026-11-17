"""Extract durable facts from one turn snapshot.

The model sees bounded, labeled sources. A citation is checked against that
set. Assistant-only claims are not stored. This module does not read a later
turn's transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...core.logger import get_logger
from ...domain.model.llm import UsageRecord
from ...domain.model.memory import (
    FACT_KINDS,
    FACT_SUBJECTS,
    SourceLocator,
    encode_locator,
    repo_space_id,
)
from ...domain.policy.memory import (
    ExtractPolicy,
    ExtractSignal,
    review_provenance,
)
from .dto import WriteRequest
from .llm_util import SideQueryResult
from .prompt import KINDS_SECTION, WHAT_NOT_TO_SAVE

logger = get_logger(__name__)

MAX_EXTRACT_CHARS = 8_000
_TRUNCATED = "…(truncated)"

EXTRACT_SYSTEM_PROMPT = f"""After a conversation between an AI agent and a user, extract facts worth keeping across sessions.

{KINDS_SECTION}

{WHAT_NOT_TO_SAVE}

Every source is labeled with an id and a kind. kind=user_statement is something the user said. kind=tool_observation is a tool observation and does not by itself mean the goal was achieved. kind=assistant_statement is an assistant statement or inference and cannot stand alone as a verified fact. kind=verification_record is only the check recorded at that time.

Extract only what is clearly worth keeping. If nothing qualifies, return an empty list.
source_refs must be ids that appear in the input. Do not invent ids.
A preference or feedback fact must cite at least one user_statement.
Do not save a conclusion supported only by assistant statements.
Facts are stored in the current project's space. Do not try to make them global.
Never extract health, money, relationship, belief, legal or identity-document details.
To replace an existing fact that is now wrong, use action=supersede with its id from the replaceable list.
markers are distinctive words (names, hostnames, code names) that would reveal this fact if they appeared elsewhere.

Output strict JSON only:
{{"facts": [
  {{"action": "create | supersede | skip", "supersedes": "f-... (supersede only)",
    "kind": "{' | '.join(FACT_KINDS)}", "subject": "{' | '.join(FACT_SUBJECTS)}",
    "content": "one self-contained statement", "why": "why it matters (optional)",
    "markers": ["optional"], "source_refs": ["source id"]}}
]}}"""


@dataclass(frozen=True)
class ExtractOutcome:
    status: str
    reason_codes: tuple[str, ...] = ()
    written: int = 0
    attempted: bool = False
    usage: UsageRecord | None = None
    duration_ms: float | None = None
    model: str = ""


def extract_from_snapshot(
    snapshot: dict[str, Any],
    *,
    query,
    directory,
    service,
    policy: ExtractPolicy | None = None,
) -> ExtractOutcome:
    """Run the gate and, when it says so, one model call. Never raises."""
    try:
        return _extract(
            snapshot,
            query=query,
            directory=directory,
            service=service,
            policy=policy or ExtractPolicy(),
        )
    except Exception as exc:
        logger.info("memory_extract failure_type=%s", type(exc).__name__)
        return ExtractOutcome("failed", (type(exc).__name__,), attempted=True)


def _extract(snapshot, *, query, directory, service, policy) -> ExtractOutcome:
    del directory
    episode = snapshot.get("episode") if isinstance(snapshot.get("episode"), dict) else {}
    project_id = str(episode.get("project_id") or "")
    if not project_id:
        return ExtractOutcome("skipped", ("missing_project_context",))
    signals, verification, kinds = _sources(snapshot)
    if not kinds:
        return ExtractOutcome("skipped", ("snapshot_missing_sources",))
    decision = policy.decide(signals, verification=verification)
    if not decision.should_extract:
        return ExtractOutcome("skipped", decision.reason_codes or ("skipped",))
    manifest, updatable_ids, readonly = _extraction_manifest(service, project_id)
    evidence = episode.get("evidence") if isinstance(episode.get("evidence"), list) else []
    packet, included_ids, offered_ids = _packet(
        evidence,
        kinds,
        _manifest_rows(manifest, updatable_ids),
        readonly,
    )
    included_kinds = {item: kinds[item] for item in included_ids}
    result: SideQueryResult = query(EXTRACT_SYSTEM_PROMPT, packet)
    if result.failed:
        return ExtractOutcome(
            "failed",
            (result.failure_type or "request_failed",),
            attempted=True,
            usage=result.usage,
            duration_ms=result.duration_ms,
            model=result.model,
        )
    try:
        payload = json.loads(result.content)
    except (TypeError, json.JSONDecodeError):
        return ExtractOutcome(
            "invalid",
            ("invalid_json",),
            attempted=True,
            usage=result.usage,
            duration_ms=result.duration_ms,
            model=result.model,
        )
    memories = payload.get("facts", []) if isinstance(payload, dict) else None
    if not isinstance(memories, list):
        return ExtractOutcome(
            "invalid",
            ("invalid_shape",),
            attempted=True,
            usage=result.usage,
            duration_ms=result.duration_ms,
            model=result.model,
        )
    if not memories:
        return ExtractOutcome(
            "empty",
            ("model_returned_empty",),
            attempted=True,
            usage=result.usage,
            duration_ms=result.duration_ms,
            model=result.model,
        )
    written, _rejected, reasons = _save_memories(
        memories,
        included_kinds,
        service,
        project_id=project_id,
        updatable_ids=offered_ids,
        evidence=evidence,
        episode=episode,
    )
    if written == 0:
        return ExtractOutcome(
            "invalid",
            tuple(reasons or ("rejected",)),
            attempted=True,
            usage=result.usage,
            duration_ms=result.duration_ms,
            model=result.model,
        )
    return ExtractOutcome(
        "succeeded",
        tuple(reasons),
        written=written,
        attempted=True,
        usage=result.usage,
        duration_ms=result.duration_ms,
        model=result.model,
    )


def _sources(snapshot: dict[str, Any]):
    episode = snapshot.get("episode") if isinstance(snapshot, dict) else None
    if not isinstance(episode, dict):
        return [], [], {}
    evidence = episode.get("evidence") or []
    if not isinstance(evidence, list):
        return [], [], {}
    signals: list[ExtractSignal] = []
    kinds: dict[str, str] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        evidence_id = item.get("id")
        kind = item.get("kind")
        if not isinstance(evidence_id, str) or not isinstance(kind, str):
            continue
        kinds[evidence_id] = kind
        if kind == "user_statement":
            signals.append(ExtractSignal(
                evidence_id=evidence_id,
                kind=kind,
                text=str(item.get("summary") or ""),
            ))
        elif kind == "tool_observation":
            ok = item.get("ok", None)
            signals.append(ExtractSignal(
                evidence_id=evidence_id,
                kind=kind,
                text=str(item.get("summary") or ""),
                command=str(item.get("command") or ""),
                subject=str(item.get("subject") or ""),
                execution_status=str(item.get("execution_status") or ""),
                ok=ok if isinstance(ok, bool) else None,
                error=str(item.get("error_excerpt") or ""),
            ))
    verification = _verification(episode, evidence)
    for item in verification:
        evidence_id = item.get("evidence_id")
        if isinstance(evidence_id, str) and evidence_id not in kinds:
            kinds[evidence_id] = "verification_record"
    return signals, verification, kinds


def _verification(episode: dict[str, Any], evidence: list[Any]) -> list[dict[str, Any]]:
    rows = episode.get("verification") or []
    if not isinstance(rows, list):
        return []
    ver_ids = [
        item.get("id")
        for item in evidence
        if isinstance(item, dict) and item.get("kind") == "verification_record"
    ]
    rendered: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if index < len(ver_ids) and isinstance(ver_ids[index], str):
            item["evidence_id"] = ver_ids[index]
        rendered.append(item)
    return rendered


def _manifest_rows(manifest: str, updatable_ids: tuple[str, ...]) -> list[tuple[str, str]]:
    """Keep only whole manifest lines whose id was offered as updatable."""
    allowed = set(updatable_ids)
    rows: list[tuple[str, str]] = []
    for line in manifest.splitlines():
        if not line.startswith("- "):
            continue
        memory_id = line[2:].split("|", 1)[0].strip()
        if memory_id in allowed:
            rows.append((memory_id, line))
    return rows


def _packet(
    evidence: list[Any],
    kinds: dict[str, str],
    updatable: list[tuple[str, str]],
    readonly: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Fit whole source entries, then whole updatable lines, into the char budget.

    An entry that is completely omitted cannot be cited. A single oversized
    entry may be truncated in place and keeps its id.
    """
    entries: list[tuple[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        evidence_id = item.get("id")
        kind = item.get("kind")
        if not isinstance(evidence_id, str) or evidence_id not in kinds:
            continue
        summary = " ".join(str(item.get("summary") or "").split())
        if kind == "assistant_statement":
            line = f"[{evidence_id} kind=assistant_statement] Assistant statement, not a verified fact: {summary}"
        else:
            line = f"[{evidence_id} kind={kind}] {summary}"
        entries.append((evidence_id, line))
    readonly_text = readonly if len(readonly) <= 1_500 else readonly[:1_500] + _TRUNCATED
    readonly_block = "Read-only global facts (cannot be replaced from here):\n" + readonly_text + "\n\n"
    source_header = "Sources (cite only ids that appear below; do not invent ids):\n"
    omitted = "Some sources were omitted for budget. Ids that do not appear cannot be cited.\n"
    intro = "Replaceable facts (supersede only these ids; they belong to the current project):\n"

    def pack(update_lines: list[str], source_lines: list[str], dropped: bool) -> str:
        update_body = "\n".join(update_lines) if update_lines else "(none)"
        text = intro + update_body + "\n\n" + readonly_block + source_header + "".join(source_lines)
        if dropped:
            text += omitted
        return text

    chosen_sources: list[tuple[str, str]] = []
    source_lines: list[str] = []
    dropped_sources = False
    for evidence_id, line in entries:
        trial_lines = [*source_lines, line + "\n"]
        # Leave room for at least the headers. Updatable lines are added after.
        skeleton = pack(["(none)"], trial_lines, dropped=False)
        if len(skeleton) <= MAX_EXTRACT_CHARS:
            chosen_sources.append((evidence_id, line))
            source_lines = trial_lines
            continue
        if not chosen_sources:
            room = MAX_EXTRACT_CHARS - len(pack(["(none)"], [], dropped=False)) - len(_TRUNCATED) - 1
            if room > 80:
                clipped = line[:room] + _TRUNCATED
                chosen_sources.append((evidence_id, clipped))
                source_lines = [clipped + "\n"]
        dropped_sources = True
        break
    if len(chosen_sources) < len(entries):
        dropped_sources = True

    chosen_updates: list[str] = []
    offered: list[str] = []
    for memory_id, line in updatable:
        trial = pack([*chosen_updates, line], source_lines, dropped_sources)
        if len(trial) > MAX_EXTRACT_CHARS:
            break
        chosen_updates.append(line)
        offered.append(memory_id)
    text = pack(chosen_updates, source_lines, dropped_sources)
    if len(text) > MAX_EXTRACT_CHARS:
        text = text[: MAX_EXTRACT_CHARS - len(_TRUNCATED)] + _TRUNCATED
        # A last-resort clip can hide a source id. Drop every id past the clip.
        visible = set()
        for evidence_id, _line in chosen_sources:
            if f"[{evidence_id} " in text or f"[{evidence_id}]" in text:
                visible.add(evidence_id)
        chosen_sources = [item for item in chosen_sources if item[0] in visible]
        offered = [item for item in offered if item in text]
    return text, tuple(item[0] for item in chosen_sources), tuple(offered)


def _locator_token(item: dict[str, Any], episode: dict[str, Any]) -> str | None:
    """Map one short id onto the session that actually produced it."""
    session_id = str(item.get("session_id") or episode.get("session_id") or "")
    root_run_id = str(item.get("root_run_id") or episode.get("root_run_id") or "")
    kind = str(item.get("kind") or "")
    message_id = str(item.get("message_id") or "")
    tool_call_id = str(item.get("tool_call_id") or "")
    step_id = str(item.get("step_id") or "")
    local_id = str(item.get("id") or "")
    if not session_id or not root_run_id or not kind or not local_id:
        return None
    if not (message_id or tool_call_id or step_id):
        return None
    return encode_locator(SourceLocator(
        session_id=session_id,
        root_run_id=root_run_id,
        run_id=str(item.get("run_id") or ""),
        kind=kind,
        message_id=message_id,
        tool_call_id=tool_call_id,
        step_id=step_id,
        episode_id=str(episode.get("id") or ""),
        local_id=local_id,
    ))


def _extraction_manifest(service, project_id: str) -> tuple[str, tuple[str, ...], str]:
    space_id = repo_space_id(project_id)
    service.ensure_repo_space(project_id)
    project_facts = service.list_facts(space_ids=[space_id])
    global_facts = service.list_facts(space_ids=["global"])
    manifest = "\n".join(f"- {item.id} | {item.kind} | {item.summary}" for item in project_facts)
    readonly = "\n".join(f"- {item.id} | {item.kind} | {item.summary}" for item in global_facts) or "(none)"
    return manifest, tuple(item.id for item in project_facts), readonly


def _save_memories(
    memories: list[Any],
    kinds: dict[str, str],
    service,
    *,
    project_id: str,
    updatable_ids: tuple[str, ...],
    evidence: list[Any],
    episode: dict[str, Any],
) -> tuple[int, int, list[str]]:
    written = 0
    rejected = 0
    reasons: list[str] = []
    by_id = {
        item.get("id"): item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    offered = set(updatable_ids)
    space_id = repo_space_id(project_id)
    episode_id = str(episode.get("id") or "")
    for item in memories:
        if written >= 5:
            break
        if not isinstance(item, dict):
            rejected += 1
            reasons.append("invalid_item")
            continue
        action = item.get("action")
        if action == "skip":
            continue
        if action not in {"create", "supersede"}:
            rejected += 1
            reasons.append("invalid_action")
            continue
        kind = item.get("kind")
        content = item.get("content")
        if not (isinstance(content, str) and content.strip()):
            rejected += 1
            reasons.append("invalid_item")
            continue
        if kind not in FACT_KINDS:
            rejected += 1
            reasons.append("invalid_kind")
            continue
        refs = item.get("source_refs")
        review = review_provenance(
            kind=str(kind),
            source_refs=refs if isinstance(refs, list) else [],
            kinds=kinds,
        )
        if not review.accept:
            rejected += 1
            reasons.append(review.reason or "rejected")
            continue
        tokens: list[str] = []
        incomplete = False
        for ref in review.source_refs:
            source = by_id.get(ref)
            token = _locator_token(source, episode) if isinstance(source, dict) else None
            if token is None:
                incomplete = True
                break
            tokens.append(token)
        if incomplete:
            rejected += 1
            reasons.append("incomplete_locator")
            continue
        supersedes = str(item.get("supersedes") or "") if action == "supersede" else ""
        if action == "supersede" and supersedes not in offered:
            rejected += 1
            reasons.append("fact_not_in_manifest")
            continue
        markers = item.get("markers")
        subject = item.get("subject")
        outcome = service.write_fact(WriteRequest(
            content=content,
            kind=str(kind),
            space_id=space_id,
            subject=str(subject) if subject in FACT_SUBJECTS else "other",
            why=str(item.get("why") or ""),
            markers=tuple(str(value) for value in markers) if isinstance(markers, list) else (),
            supersedes=supersedes,
            sources=tuple(dict.fromkeys([episode_id, *tokens])) if episode_id else tuple(tokens),
            actor="extractor",
        ))
        if not outcome.ok:
            rejected += 1
            reasons.append(outcome.status)
            continue
        written += 1
    return written, rejected, reasons


__all__ = ["EXTRACT_SYSTEM_PROMPT", "ExtractOutcome", "extract_from_snapshot"]
