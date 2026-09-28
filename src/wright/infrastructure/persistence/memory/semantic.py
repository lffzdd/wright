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
import threading
from pathlib import Path

from ....domain.gateway.memory import ISemanticMemoryStore
from ....domain.model.memory import (
    PROJECT_ID_RE,
    SEMANTIC_SCHEMA_VERSION,
    SemanticMemoryAlreadyExistsError,
    SemanticMemoryConflictError,
    SemanticMemoryRecord,
    SemanticMemoryStoreError,
    SemanticMemoryType,
    SourceLocator,
    parse_semantic_memory_type,
    parse_semantic_status,
)
from .semantic_document import (
    _atomic_write,
    _encode_refs,
    _load_path,
    _revision,
    _utc_now,
)
from .semantic_document import (
    dump_frontmatter as _dump_frontmatter,
)
from .semantic_document import (
    new_memory_id as _new_memory_id,
)
from .semantic_document import (
    normalize_memory_id as _normalize_memory_id,
)
from .semantic_document import (
    slugify as _slugify,
)
from .semantic_index import (
    _directory,
    _matching_headers,
    _rebuild_index_unlocked,
    _require_visible,
    _resolve_path,
)
from .semantic_index import (
    read_entrypoint as _read_entrypoint,
)

_locks_guard = threading.Lock()
_directory_locks: dict[Path, threading.RLock] = {}


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
    chosen = _normalize_memory_id(memory_id) if memory_id else _new_memory_id()
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
        _atomic_write(path, _dump_frontmatter(record))
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
    _atomic_write(path, _dump_frontmatter(updated))
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
        memory_id=_slugify(name),
    )
    return record.path


def rebuild_index(directory: Path | None = None) -> Path:
    directory = _directory(directory)
    with _StoreLock(directory):
        return _rebuild_index_unlocked(directory)


class SemanticMemoryStore(ISemanticMemoryStore):
    """Markdown semantic memories. Create and update are different operations."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = _directory(directory)

    def read_index(self) -> str:
        return _read_entrypoint(self.directory)

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
    "SemanticMemoryStore",
    "create_memory",
    "delete_memory",
    "get_memory",
    "list_memories",
    "rebuild_index",
    "search_memories",
    "update_memory",
    "write_memory_file",
]
