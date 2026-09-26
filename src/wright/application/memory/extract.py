"""Extract durable semantic memories from one turn snapshot.

The model sees bounded, labeled sources. A citation is checked against that
set. Assistant-only claims are not stored. This module does not read a later
turn's transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...core.logger import get_logger
from ...domain.model.memory import (
    SEMANTIC_MEMORY_TYPES,
    TYPES_SECTION,
    WHAT_NOT_TO_SAVE,
)
from ...domain.model.session import UsageRecord
from ...domain.policy.memory import (
    ExtractSignal,
    SemanticExtractPolicy,
    review_provenance,
)
from ...infrastructure.persistence.memory import format_manifest, scan_memory_files
from .llm_util import SideQueryResult

logger = get_logger(__name__)

MAX_EXTRACT_CHARS = 8_000
_TRUNCATED = "…(已截断)"

EXTRACT_SYSTEM_PROMPT = f"""你在一段 AI Agent 与用户的对话结束后，从标注来源里提取值得【长期、跨会话】保留的记忆。

{TYPES_SECTION}

{WHAT_NOT_TO_SAVE}

每条来源都标了 id 和 kind。kind=user_statement 是用户陈述。kind=tool_observation 是工具观察，不自动等于目标已经达成。kind=assistant_statement 是助手陈述或推断，不能单独当作已验证事实。kind=verification_record 只表示当时的检查记录。

请只提取明显值得保留的内容。没有就返回空列表。
source_refs 必须是输入里出现过的 id，不能编造。
user 或 feedback 必须引用至少一条 user_statement。
只有助手陈述支持的结论不要保存。
update 时填写已有清单中的 memory_id；create 时不要填写 memory_id。

只输出严格 JSON:
{{"memories": [
  {{"memory_id": "已有-id（仅 update）", "name": "简短主题名", "description": "一句话描述",
    "type": "{' | '.join(SEMANTIC_MEMORY_TYPES)}",
    "content": "记忆正文", "action": "create | update | skip",
    "source_refs": ["来源 id"]}}
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
    policy: SemanticExtractPolicy | None = None,
) -> ExtractOutcome:
    """Run the gate and, when it says so, one model call. Never raises."""
    try:
        return _extract(
            snapshot,
            query=query,
            directory=directory,
            service=service,
            policy=policy or SemanticExtractPolicy(),
        )
    except Exception as exc:
        logger.info("memory_extract failure_type=%s", type(exc).__name__)
        return ExtractOutcome("failed", (type(exc).__name__,), attempted=True)


def _extract(snapshot, *, query, directory, service, policy) -> ExtractOutcome:
    signals, verification, kinds = _sources(snapshot)
    if not kinds:
        return ExtractOutcome("skipped", ("snapshot_missing_sources",))
    decision = policy.decide(signals, verification=verification)
    if not decision.should_extract:
        return ExtractOutcome("skipped", decision.reason_codes or ("skipped",))
    packet = _packet(snapshot, kinds, directory)
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
    memories = payload.get("memories", []) if isinstance(payload, dict) else None
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
    written, _rejected, reasons = _save_memories(memories, kinds, service)
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


def _packet(snapshot: dict[str, Any], kinds: dict[str, str], directory) -> str:
    episode = snapshot.get("episode") or {}
    evidence = episode.get("evidence") or []
    lines = ["来源（只能引用这些 id，不能编造）:"]
    for item in evidence:
        if not isinstance(item, dict):
            continue
        evidence_id = item.get("id")
        kind = item.get("kind")
        if evidence_id not in kinds:
            continue
        summary = " ".join(str(item.get("summary") or "").split())
        if kind == "assistant_statement":
            lines.append(
                f"[{evidence_id} kind=assistant_statement] 助手陈述，不是已验证事实: {summary}"
            )
        else:
            lines.append(f"[{evidence_id} kind={kind}] {summary}")
    try:
        manifest = format_manifest(scan_memory_files(directory)) or "(暂无)"
    except Exception:
        manifest = "(暂无)"
    if len(manifest) > 2_000:
        manifest = manifest[:2_000] + _TRUNCATED
    text = "已有记忆清单:\n" + manifest + "\n\n" + "\n".join(lines)
    if len(text) > MAX_EXTRACT_CHARS:
        text = text[: MAX_EXTRACT_CHARS - len(_TRUNCATED)] + _TRUNCATED
    return text


def _save_memories(memories: list[Any], kinds: dict[str, str], service) -> tuple[int, int, list[str]]:
    written = 0
    rejected = 0
    reasons: list[str] = []
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
        if action not in {"create", "update"}:
            rejected += 1
            reasons.append("invalid_action")
            continue
        name = item.get("name")
        type_ = item.get("type")
        content = item.get("content")
        refs = item.get("source_refs")
        if not (isinstance(name, str) and name and isinstance(content, str) and content):
            rejected += 1
            reasons.append("invalid_item")
            continue
        if type_ not in SEMANTIC_MEMORY_TYPES:
            rejected += 1
            reasons.append("invalid_type")
            continue
        review = review_provenance(
            memory_type=str(type_),
            source_refs=refs if isinstance(refs, list) else [],
            kinds=kinds,
        )
        if not review.accept:
            rejected += 1
            reasons.append(review.reason or "rejected")
            continue
        _record, error = service.record_extracted(
            name=str(name),
            content=str(content),
            type_=str(type_),
            description=str(item.get("description") or ""),
            origin=review.origin,
            source_refs=review.source_refs,
            memory_id=str(item.get("memory_id") or "") if action == "update" else "",
            action=action,
        )
        if error is not None:
            rejected += 1
            reasons.append("write_rejected")
            continue
        written += 1
    return written, rejected, reasons


__all__ = ["EXTRACT_SYSTEM_PROMPT", "ExtractOutcome", "extract_from_snapshot"]
