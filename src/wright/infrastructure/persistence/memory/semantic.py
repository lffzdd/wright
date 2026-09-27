"""File-backed semantic memory: one markdown record per durable fact.

Identity is the stable id, not the display name. Scope is stored explicitly.
Markdown that is not schema version 1 is ignored. It is not classified, migrated,
or treated as global. The human ``MEMORY.md`` index is derived; agent recall
must build its own scoped view.
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ....domain.gateway.memory import ISemanticMemoryStore
from ....domain.model.memory import (
    PROJECT_ID_RE,
    SEMANTIC_SCHEMA_VERSION,
    SemanticMemoryAlreadyExistsError,
    SemanticMemoryConflictError,
    SemanticMemoryHeader,
    SemanticMemoryNotFoundError,
    SemanticMemoryRecord,
    SemanticMemoryScopeError,
    SemanticMemoryStoreError,
    SemanticMemoryType,
    SourceLocator,
    encode_locator,
    parse_locator,
    parse_semantic_memory_type,
    parse_semantic_status,
)
from ....domain.policy.memory import normalize_stored_scope, record_in_read_scope, scope_denial_message
from .paths import MEMORY_INDEX, entrypoint_path, memory_dir

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
FRONTMATTER_MAX_LINES = 80
MAX_MEMORY_CHARS = 4_000
MAX_MEMORY_NAME_CHARS = 120
MAX_MEMORY_DESCRIPTION_CHARS = 500
MAX_MEMORY_CONTENT_CHARS = 12_000

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_SAFE_ID_RE = re.compile(r"[\w-]{1,160}", re.UNICODE)
_locks_guard = threading.Lock()
_directory_locks: dict[Path, threading.RLock] = {}


def _directory(directory: Path | None) -> Path:
    return (directory or memory_dir()).expanduser().resolve()


def _thread_lock(directory: Path) -> threading.RLock:
    with _locks_guard:
        return _directory_locks.setdefault(directory, threading.RLock())


class _StoreLock:
    """In-process re-entrant lock plus an exclusive inter-process file lock."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._thread = _thread_lock(directory)
        self._fd: int | None = None

    def __enter__(self) -> _StoreLock:
        self._thread.acquire()
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.directory / ".semantic.lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._fd = fd
        except Exception:
            self._thread.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
        finally:
            self._thread.release()


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


def _read_head(path: Path, max_lines: int) -> str:
    lines: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index >= max_lines:
                break
            lines.append(line)
    return "".join(lines)


def _is_memory_file(path: Path) -> bool:
    return (
        path.name != MEMORY_INDEX
        and not path.name.startswith(".")
        and path.suffix == ".md"
        and path.is_file()
        and not path.is_symlink()
    )


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


def _header_from_path(path: Path) -> SemanticMemoryHeader | None:
    try:
        head = _read_head(path, FRONTMATTER_MAX_LINES)
        fm, _body = parse_frontmatter(head)
        stat = path.stat()
        memory_id, scope, project, status, revision = _current_fields(path, fm)
    except (OSError, SemanticMemoryStoreError):
        return None
    return SemanticMemoryHeader(
        id=memory_id,
        filename=path.name,
        path=path,
        mtime=stat.st_mtime,
        name=fm.get("name") or path.stem,
        description=fm.get("description") or None,
        type=parse_semantic_memory_type(fm.get("type")),
        scope=scope,  # type: ignore[arg-type]
        created_at=fm.get("created_at"),
        updated_at=fm.get("updated_at"),
        project_id=project,
        status=status,  # type: ignore[arg-type]
        revision=revision,
    )


def scan_memory_files(directory: Path | None = None) -> list[SemanticMemoryHeader]:
    """Scan every memory file. Callers filter scope before they truncate."""
    directory = _directory(directory)
    if not directory.is_dir():
        return []
    headers: list[SemanticMemoryHeader] = []
    for path in directory.iterdir():
        if not _is_memory_file(path):
            continue
        header = _header_from_path(path)
        if header is not None:
            headers.append(header)
    headers.sort(key=lambda header: header.mtime, reverse=True)
    return headers


def format_manifest(headers: list[SemanticMemoryHeader]) -> str:
    lines: list[str] = []
    for header in headers:
        tag = f"[{header.type}] " if header.type else ""
        desc = f": {header.description}" if header.description else ""
        lines.append(
            f"- {tag}{header.filename}{desc} id={header.id} scope={header.scope}"
        )
    return "\n".join(lines)


def _source_tokens(fm: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        line.strip() for line in fm.get("source_ref", "").splitlines() if line.strip()
    )


def _encode_refs(
    source_refs: tuple[str, ...],
) -> tuple[tuple[SourceLocator, ...], tuple[str, ...]]:
    locators = tuple(parse_locator(token) for token in source_refs)
    return locators, tuple(encode_locator(locator) for locator in locators)


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


def _resolve_path(memory_id: str, directory: Path) -> Path:
    normalized = normalize_memory_id(memory_id)
    path = directory / f"{normalized}.md"
    if not _is_memory_file(path):
        raise SemanticMemoryNotFoundError(f"记忆不存在: {normalized}")
    return path


def _visible(record: SemanticMemoryRecord, *, read_scope: str, project_id: str, include_inactive: bool) -> bool:
    return record_in_read_scope(
        scope=record.scope,
        project_id=record.project_id,
        status=record.status,
        read_scope=read_scope,
        current_project_id=project_id,
        include_inactive=include_inactive,
    )


def _require_visible(
    record: SemanticMemoryRecord,
    *,
    read_scope: str,
    project_id: str,
) -> None:
    if _visible(record, read_scope=read_scope, project_id=project_id, include_inactive=True):
        return
    raise SemanticMemoryScopeError(
        scope_denial_message(scope=record.scope, status=record.status, read_scope=read_scope)
    )


def _matching_headers(
    directory: Path,
    *,
    read_scope: str,
    project_id: str,
    include_inactive: bool,
    type_: str | None,
) -> list[SemanticMemoryHeader]:
    if type_ is not None and parse_semantic_memory_type(type_) is None:
        raise SemanticMemoryStoreError(f"非法 memory type: {type_}")
    matched: list[SemanticMemoryHeader] = []
    for header in scan_memory_files(directory):
        if type_ is not None and header.type != type_:
            continue
        if not record_in_read_scope(
            scope=header.scope,
            project_id=header.project_id,
            status=header.status,
            read_scope=read_scope,
            current_project_id=project_id,
            include_inactive=include_inactive,
        ):
            continue
        matched.append(header)
    matched.sort(key=lambda header: (header.updated_at or "", header.id), reverse=True)
    return matched


def get_memory(memory_id: str, directory: Path | None = None) -> SemanticMemoryRecord:
    directory = _directory(directory)
    with _StoreLock(directory):
        return _load_path(_resolve_path(memory_id, directory))


def create_memory(
    name: str,
    description: str,
    type_: str,
    content: str,
    directory: Path | None = None,
    *,
    scope: str = "project",
    project_id: str = "",
    origin: str = "",
    source_refs: tuple[str, ...] = (),
    memory_id: str = "",
) -> SemanticMemoryRecord:
    directory = _directory(directory)
    if scope == "project":
        if not project_id or PROJECT_ID_RE.fullmatch(project_id) is None:
            raise SemanticMemoryStoreError("缺少项目上下文，不能写入项目记忆")
    elif scope == "global":
        project_id = ""
    else:
        raise SemanticMemoryStoreError("新建记忆的 scope 只能是 project 或 global")
    memory_type = parse_semantic_memory_type(type_)
    if memory_type is None:
        raise SemanticMemoryStoreError(f"非法 memory type: {type_}")
    chosen = normalize_memory_id(memory_id) if memory_id else new_memory_id()
    locators, tokens = _encode_refs(source_refs)
    now = _utc_now()
    with _StoreLock(directory):
        path = (directory / f"{chosen}.md").resolve()
        if path.parent != directory:
            raise SemanticMemoryStoreError("memory path 越界")
        if path.exists():
            raise SemanticMemoryAlreadyExistsError(f"记忆已存在: {chosen}")
        record = SemanticMemoryRecord(
            id=chosen,
            name=name,
            description=description,
            type=memory_type,
            content=content,
            created_at=now,
            updated_at=now,
            path=path,
            scope=scope,  # type: ignore[arg-type]
            origin=origin,
            source_refs=tokens,
            schema_version=SEMANTIC_SCHEMA_VERSION,
            project_id=project_id,
            status="active",
            revision=1,
            locators=locators,
        )
        _atomic_write(path, dump_frontmatter(record))
        _rebuild_index_unlocked(directory)
        return _load_path(path)


_UNSET = object()


def update_memory(
    memory_id: str,
    *,
    expected_revision: int,
    directory: Path | None = None,
    read_scope: str = "applicable",
    project_id: str = "",
    name: str | None = None,
    description: str | None = None,
    type_: str | None = None,
    content: str | None = None,
    status: str | None = None,
    origin: str | object = _UNSET,
    source_refs: tuple[str, ...] | object = _UNSET,
) -> SemanticMemoryRecord:
    directory = _directory(directory)
    revision = _revision(expected_revision)
    if status is not None and parse_semantic_status(status) is None:
        raise SemanticMemoryStoreError(f"非法 memory status: {status}")
    provenance_set = origin is not _UNSET or source_refs is not _UNSET
    with _StoreLock(directory):
        path = _resolve_path(memory_id, directory)
        current = _load_path(path)
        if current.revision != revision:
            raise SemanticMemoryConflictError(
                f"记忆已被更新，当前 revision={current.revision}，拒绝用旧版本覆盖",
                current_revision=current.revision,
            )
        _require_visible(current, read_scope=read_scope, project_id=project_id)
        return _apply_update_unlocked(
            directory,
            path,
            current,
            name=name,
            description=description,
            type_=type_,
            content=content,
            status=status,
            origin=None if origin is _UNSET else str(origin),
            source_refs=None if source_refs is _UNSET else tuple(source_refs),  # type: ignore[arg-type]
            provenance_set=provenance_set,
        )


def _apply_update_unlocked(
    directory: Path,
    path: Path,
    current: SemanticMemoryRecord,
    *,
    name: str | None,
    description: str | None,
    type_: str | None,
    content: str | None,
    status: str | None,
    origin: str | None,
    source_refs: tuple[str, ...] | None,
    provenance_set: bool,
) -> SemanticMemoryRecord:
    next_name = current.name if name is None else name
    next_description = current.description if description is None else description
    next_type = current.type if type_ is None else type_
    next_content = current.content if content is None else content
    next_status = current.status if status is None else status
    content_changed = next_content != current.content
    if content_changed and not provenance_set:
        next_origin = ""
        next_refs: tuple[str, ...] = ()
        next_locators: tuple[SourceLocator, ...] = ()
    elif provenance_set:
        next_origin = current.origin if origin is None else origin
        raw_refs = current.source_refs if source_refs is None else source_refs
        next_locators, next_refs = _encode_refs(raw_refs)
    else:
        next_origin = current.origin
        next_refs = current.source_refs
        next_locators = current.locators
    unchanged = (
        next_name == current.name
        and next_description == current.description
        and next_type == current.type
        and next_content == current.content
        and next_status == current.status
        and next_origin == current.origin
        and next_refs == current.source_refs
    )
    if unchanged:
        return current
    memory_type = parse_semantic_memory_type(next_type)
    if memory_type is None:
        raise SemanticMemoryStoreError(f"非法 memory type: {next_type}")
    updated = SemanticMemoryRecord(
        id=current.id,
        name=next_name,
        description=next_description,
        type=memory_type,
        content=next_content,
        created_at=current.created_at,
        updated_at=_utc_now(),
        path=path,
        scope=current.scope,
        origin=next_origin,
        source_refs=next_refs,
        schema_version=SEMANTIC_SCHEMA_VERSION,
        project_id=current.project_id,
        status=next_status,  # type: ignore[arg-type]
        revision=current.revision + 1,
        locators=next_locators,
    )
    _atomic_write(path, dump_frontmatter(updated))
    _rebuild_index_unlocked(directory)
    return _load_path(path)


def delete_memory(
    memory_id: str,
    directory: Path | None = None,
    *,
    read_scope: str = "applicable",
    project_id: str = "",
) -> SemanticMemoryRecord:
    directory = _directory(directory)
    with _StoreLock(directory):
        path = _resolve_path(memory_id, directory)
        current = _load_path(path)
        _require_visible(current, read_scope=read_scope, project_id=project_id)
        try:
            path.unlink()
        except OSError as exc:
            raise SemanticMemoryStoreError(f"删除记忆失败: {exc}") from exc
        _rebuild_index_unlocked(directory)
    return current


def search_memories(
    query: str = "",
    *,
    type_: str | None = None,
    limit: int = 20,
    directory: Path | None = None,
    read_scope: str = "applicable",
    project_id: str = "",
    include_inactive: bool = False,
) -> list[SemanticMemoryRecord]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise SemanticMemoryStoreError("limit 必须是 1..100 的整数")
    directory = _directory(directory)
    query_text = str(query).strip().casefold()
    terms = [term for term in re.split(r"\s+", query_text) if term]
    with _StoreLock(directory):
        headers = _matching_headers(
            directory,
            read_scope=read_scope,
            project_id=project_id,
            include_inactive=include_inactive,
            type_=type_,
        )
        scored: list[tuple[int, str, SemanticMemoryRecord]] = []
        for header in headers:
            try:
                record = _load_path(header.path)
            except SemanticMemoryStoreError:
                continue
            haystack = (
                f"{record.id}\n{record.name}\n{record.description}\n"
                f"{record.type}\n{record.scope}\n{record.content}"
            ).casefold()
            if terms and not all(term in haystack for term in terms):
                continue
            score = sum(haystack.count(term) for term in terms) if terms else 0
            scored.append((score, record.updated_at, record))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [record for _, _, record in scored[:limit]]


def list_memories(
    *,
    limit: int = 100,
    directory: Path | None = None,
    read_scope: str = "applicable",
    project_id: str = "",
    include_inactive: bool = False,
    type_: str | None = None,
) -> list[SemanticMemoryRecord]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise SemanticMemoryStoreError("limit 必须是 1..500 的整数")
    directory = _directory(directory)
    with _StoreLock(directory):
        headers = _matching_headers(
            directory,
            read_scope=read_scope,
            project_id=project_id,
            include_inactive=include_inactive,
            type_=type_,
        )
        records: list[SemanticMemoryRecord] = []
        for header in headers[:limit]:
            try:
                records.append(_load_path(header.path))
            except SemanticMemoryStoreError:
                continue
        return records


def write_memory_file(
    name: str,
    description: str,
    type_: str,
    content: str,
    directory: Path | None = None,
    *,
    scope: str = "global",
    project_id: str = "",
) -> Path:
    """Write one fixture or hand-authored file.

    The default scope is the explicit value ``global``. Product creates do not
    use this helper; they go through ``create_memory`` and require a project
    unless scope is global. The slug is only this helper's id. A second call
    with the same name fails instead of overwriting.
    """
    record = create_memory(
        name,
        description,
        type_,
        content,
        directory,
        scope=scope,
        project_id=project_id,
        memory_id=slugify(name),
    )
    return record.path


def rebuild_index(directory: Path | None = None) -> Path:
    directory = _directory(directory)
    with _StoreLock(directory):
        return _rebuild_index_unlocked(directory)


def _rebuild_index_unlocked(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    headers = scan_memory_files(directory)
    lines = [
        "# MEMORY.md",
        "",
        "给人看的全库导航，不是 Agent 的召回真源。",
        "自动上下文只使用全局和当前项目里 status=active 的记录。",
        "不是当前格式的 Markdown 不会出现在这里。",
        "",
    ]
    for header in headers:
        desc = f" — {header.description}" if header.description else ""
        tag = f" `{header.type}`" if header.type else ""
        project = f" project={header.project_id}" if header.project_id else ""
        lines.append(
            f"- [{header.name}]({header.filename}){desc}{tag}"
            f" scope={header.scope} status={header.status}{project}"
        )
    if not headers:
        lines.append("_(暂无记忆)_")
    index_path = directory / MEMORY_INDEX
    _atomic_write(index_path, "\n".join(lines) + "\n")
    return index_path


def read_entrypoint(directory: Path | None = None) -> str:
    """Read the human index. Agent recall must not inject this text blindly."""
    path = (_directory(directory) / MEMORY_INDEX) if directory else entrypoint_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not raw:
        return ""
    lines = raw.split("\n")
    truncated = False
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
        truncated = True
    out = "\n".join(lines)
    if len(out.encode("utf-8")) > MAX_INDEX_BYTES:
        out = out.encode("utf-8")[:MAX_INDEX_BYTES].decode("utf-8", "ignore")
        truncated = True
    if truncated:
        out += (
            f"\n\n> 警告:{MEMORY_INDEX} 超出上限,仅加载了部分。"
            "请把索引条目压到一行、细节移进各自的记忆文件。"
        )
    return out


def read_memories_for_surfacing(paths: list[Path]) -> str:
    blocks: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if len(text) > MAX_MEMORY_CHARS:
            text = text[:MAX_MEMORY_CHARS] + "\n…(已截断)"
        blocks.append(f"### {path.name}\n{text}")
    return "\n\n".join(blocks)


class SemanticMemoryStore(ISemanticMemoryStore):
    """Markdown semantic memories. Create and update are different operations."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = _directory(directory)

    def read_index(self) -> str:
        return read_entrypoint(self.directory)

    def list(
        self,
        limit: int = 100,
        *,
        read_scope: str = "applicable",
        project_id: str = "",
        include_inactive: bool = False,
        type_: SemanticMemoryType | None = None,
    ) -> list[SemanticMemoryRecord]:
        return list_memories(
            limit=limit,
            directory=self.directory,
            read_scope=read_scope,
            project_id=project_id,
            include_inactive=include_inactive,
            type_=type_,
        )

    def get(self, memory_id: str) -> SemanticMemoryRecord:
        return get_memory(memory_id, self.directory)

    def search(
        self,
        query: str = "",
        *,
        limit: int = 20,
        read_scope: str = "applicable",
        project_id: str = "",
        include_inactive: bool = False,
        type_: SemanticMemoryType | None = None,
    ) -> list[SemanticMemoryRecord]:
        return search_memories(
            query,
            type_=type_,
            limit=limit,
            directory=self.directory,
            read_scope=read_scope,
            project_id=project_id,
            include_inactive=include_inactive,
        )

    def create(
        self,
        *,
        name: str,
        description: str,
        type_: SemanticMemoryType,
        content: str,
        scope: str,
        project_id: str = "",
        origin: str = "",
        source_refs: tuple[str, ...] = (),
        memory_id: str = "",
    ) -> SemanticMemoryRecord:
        return create_memory(
            name,
            description,
            type_,
            content,
            self.directory,
            scope=scope,
            project_id=project_id,
            origin=origin,
            source_refs=source_refs,
            memory_id=memory_id,
        )

    def update(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        read_scope: str = "applicable",
        project_id: str = "",
        name: str | None = None,
        description: str | None = None,
        type_: SemanticMemoryType | None = None,
        content: str | None = None,
        status: str | None = None,
        origin: str | None = None,
        source_refs: tuple[str, ...] | None = None,
        provenance_set: bool = False,
    ) -> SemanticMemoryRecord:
        return update_memory(
            memory_id,
            expected_revision=expected_revision,
            directory=self.directory,
            read_scope=read_scope,
            project_id=project_id,
            name=name,
            description=description,
            type_=type_,
            content=content,
            status=status,
            origin=_UNSET if not provenance_set else (origin or ""),
            source_refs=_UNSET if not provenance_set else tuple(source_refs or ()),
        )

    def delete(
        self,
        memory_id: str,
        *,
        read_scope: str = "applicable",
        project_id: str = "",
    ) -> SemanticMemoryRecord:
        return delete_memory(
            memory_id,
            self.directory,
            read_scope=read_scope,
            project_id=project_id,
        )


__all__ = [
    "FRONTMATTER_MAX_LINES",
    "MAX_INDEX_BYTES",
    "MAX_INDEX_LINES",
    "MAX_MEMORY_CHARS",
    "MAX_MEMORY_CONTENT_CHARS",
    "MAX_MEMORY_DESCRIPTION_CHARS",
    "MAX_MEMORY_NAME_CHARS",
    "SemanticMemoryStore",
    "SemanticMemoryStoreError",
    "create_memory",
    "delete_memory",
    "dump_frontmatter",
    "format_manifest",
    "get_memory",
    "list_memories",
    "new_memory_id",
    "normalize_memory_id",
    "parse_frontmatter",
    "read_entrypoint",
    "read_memories_for_surfacing",
    "rebuild_index",
    "scan_memory_files",
    "search_memories",
    "slugify",
    "update_memory",
    "write_memory_file",
]
