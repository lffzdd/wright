"""Markdown and frontmatter codec for one semantic memory file.

This module does not lock the store. Callers that replace a file inside a
read-modify-write hold ``_StoreLock`` themselves.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ....domain.model.memory import (
    SEMANTIC_SCHEMA_VERSION,
    SemanticMemoryNotFoundError,
    SemanticMemoryRecord,
    SemanticMemoryStoreError,
    SourceLocator,
    encode_locator,
    parse_locator,
    parse_semantic_memory_type,
    parse_semantic_status,
)
from ....domain.policy.memory import normalize_stored_scope

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
FRONTMATTER_MAX_LINES = 80
MAX_MEMORY_CHARS = 4_000
MAX_MEMORY_NAME_CHARS = 120
MAX_MEMORY_DESCRIPTION_CHARS = 500
MAX_MEMORY_CONTENT_CHARS = 12_000

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_SAFE_ID_RE = re.compile(r"[\w-]{1,160}", re.UNICODE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_fm, body = match.group(1), match.group(2)
    fm: dict[str, str] = {}
    source_refs: list[str] = []
    for line in raw_fm.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key == "source_ref":
            if value:
                source_refs.append(value)
            continue
        fm[key] = value
    if source_refs:
        fm["source_ref"] = "\n".join(source_refs)
    return fm, body

def dump_frontmatter(record: SemanticMemoryRecord) -> str:
    name = _single_line(record.name, "name", MAX_MEMORY_NAME_CHARS)
    description = _single_line(
        record.description, "description", MAX_MEMORY_DESCRIPTION_CHARS, allow_empty=True
    )
    memory_type = parse_semantic_memory_type(record.type)
    if memory_type is None:
        raise SemanticMemoryStoreError(f"非法 memory type: {record.type}")
    if record.scope not in {"global", "project"}:
        raise SemanticMemoryStoreError(f"非法 memory scope: {record.scope}")
    if record.status not in {"active", "inactive"}:
        raise SemanticMemoryStoreError(f"非法 memory status: {record.status}")
    content = _content(record.content)
    lines = [
        "---",
        f"schema_version: {record.schema_version}",
        f"id: {_single_line(record.id, 'id', 160)}",
        f"name: {name}",
        f"description: {description}",
        f"type: {memory_type}",
        f"scope: {record.scope}",
    ]
    if record.project_id:
        lines.append(f"project_id: {_single_line(record.project_id, 'project_id', 200)}")
    lines.extend([
        f"status: {record.status}",
        f"created_at: {record.created_at}",
        f"updated_at: {record.updated_at}",
        f"revision: {record.revision}",
    ])
    if record.origin or record.source_refs:
        lines.append(f"origin: {_single_line(record.origin, 'origin', 40, allow_empty=True)}")
        for token in record.source_refs:
            lines.append(f"source_ref: {_single_line(token, 'source_ref', 500)}")
    lines.extend(["---", "", content, ""])
    return "\n".join(lines)

def slugify(name: str) -> str:
    """Create a Unicode-safe slug. Punctuation-only names use a fixed fallback."""
    normalized = unicodedata.normalize("NFKC", str(name)).strip().casefold()
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE)
    slug = slug.replace("_", "-").strip("-")[:160].strip("-")
    if slug:
        return slug
    return "memory"

def new_memory_id() -> str:
    return f"mem-{uuid.uuid4().hex[:16]}"

def normalize_memory_id(memory_id: str) -> str:
    value = str(memory_id).strip()
    value = value.removesuffix(".md")
    if not value or _SAFE_ID_RE.fullmatch(value) is None:
        raise SemanticMemoryStoreError("memory_id 必须是安全的记忆 id，不能包含路径")
    return value

def _single_line(
    value: object,
    field: str,
    max_chars: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise SemanticMemoryStoreError(f"{field} 必须是字符串")
    cleaned = " ".join(value.splitlines()).strip()
    if not allow_empty and not cleaned:
        raise SemanticMemoryStoreError(f"{field} 不能为空")
    if len(cleaned) > max_chars:
        raise SemanticMemoryStoreError(f"{field} 不能超过 {max_chars} 个字符")
    return cleaned

def _content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticMemoryStoreError("content 不能为空")
    cleaned = value.strip()
    if len(cleaned) > MAX_MEMORY_CONTENT_CHARS:
        raise SemanticMemoryStoreError(
            f"content 不能超过 {MAX_MEMORY_CONTENT_CHARS} 个字符"
        )
    return cleaned

def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SemanticMemoryStoreError("expected_revision 必须是非负整数")
    return value

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

def _source_tokens(fm: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        line.strip() for line in fm.get("source_ref", "").splitlines() if line.strip()
    )

def _encode_refs(
    source_refs: tuple[str, ...],
) -> tuple[tuple[SourceLocator, ...], tuple[str, ...]]:
    locators = tuple(parse_locator(token) for token in source_refs)
    return locators, tuple(encode_locator(locator) for locator in locators)

def _current_fields(path: Path, fm: dict[str, str]) -> tuple[str, str, str, str, int]:
    """Require the current semantic schema. Older markdown is not a memory."""
    if fm.get("schema_version") != str(SEMANTIC_SCHEMA_VERSION):
        raise SemanticMemoryStoreError(f"不是当前语义记忆格式: {path.name}")
    try:
        memory_id = normalize_memory_id(fm.get("id") or "")
    except SemanticMemoryStoreError as exc:
        raise SemanticMemoryStoreError(f"不是当前语义记忆格式: {path.name}") from exc
    if memory_id != path.stem:
        raise SemanticMemoryStoreError(f"记忆 id 与文件名不一致: {path.name}")
    stored = normalize_stored_scope(fm.get("scope"), fm.get("project_id") or "")
    if stored is None:
        raise SemanticMemoryStoreError(f"不是当前语义记忆格式: {path.name}")
    scope, project = stored
    status = parse_semantic_status(fm.get("status")) if "status" in fm else None
    if status is None or "revision" not in fm or not fm.get("created_at") or not fm.get("updated_at"):
        raise SemanticMemoryStoreError(f"不是当前语义记忆格式: {path.name}")
    try:
        revision = int(fm["revision"])
    except ValueError as exc:
        raise SemanticMemoryStoreError(f"不是当前语义记忆格式: {path.name}") from exc
    if revision < 0:
        raise SemanticMemoryStoreError(f"不是当前语义记忆格式: {path.name}")
    return memory_id, scope, project, status, revision


def _load_path(path: Path) -> SemanticMemoryRecord:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SemanticMemoryNotFoundError(f"记忆不存在: {path.stem}") from exc
    fm, body = parse_frontmatter(text)
    memory_id, scope, project, status, revision = _current_fields(path, fm)
    memory_type = parse_semantic_memory_type(fm.get("type"))
    if memory_type is None:
        raise SemanticMemoryStoreError(f"记忆 {path.name} 缺少合法 type")
    locators, stored_tokens = _encode_refs(_source_tokens(fm))
    return SemanticMemoryRecord(
        id=memory_id,
        name=fm.get("name") or path.stem,
        description=fm.get("description") or "",
        type=memory_type,
        content=body.strip(),
        created_at=fm["created_at"],
        updated_at=fm["updated_at"],
        path=path,
        scope=scope,  # type: ignore[arg-type]
        origin=fm.get("origin") or "",
        source_refs=stored_tokens,
        schema_version=SEMANTIC_SCHEMA_VERSION,
        project_id=project,
        status=status,  # type: ignore[arg-type]
        revision=revision,
        locators=locators,
    )
