"""File-backed semantic memory: one markdown record per durable fact."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from ....domain.gateway.memory import ISemanticMemoryStore
from ....domain.model.memory import (
    SemanticMemoryAlreadyExistsError,
    SemanticMemoryHeader,
    SemanticMemoryNotFoundError,
    SemanticMemoryRecord,
    SemanticMemoryStoreError,
    SemanticMemoryType,
    parse_semantic_memory_type,
)
from .paths import MEMORY_INDEX, entrypoint_path, memory_dir

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
FRONTMATTER_MAX_LINES = 30
MAX_MEMORY_CHARS = 4_000
MAX_MEMORY_FILES = 200
MAX_MEMORY_NAME_CHARS = 120
MAX_MEMORY_DESCRIPTION_CHARS = 500
MAX_MEMORY_CONTENT_CHARS = 12_000

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_SAFE_ID_RE = re.compile(r"[\w-]{1,160}", re.UNICODE)
_locks_guard = threading.Lock()
_directory_locks: dict[Path, threading.RLock] = {}


def _directory(directory: Path | None) -> Path:
    return (directory or memory_dir()).expanduser().resolve()


def _lock_for(directory: Path) -> threading.RLock:
    with _locks_guard:
        return _directory_locks.setdefault(directory, threading.RLock())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw_fm, body = m.group(1), m.group(2)
    fm: dict[str, str] = {}
    for line in raw_fm.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            fm[key] = value
    return fm, body


def dump_frontmatter(
    name: str,
    description: str,
    type_: str,
    body: str,
    *,
    created_at: str | None = None,
    updated_at: str | None = None,
    origin: str = "",
    source_refs: tuple[str, ...] = (),
) -> str:
    name = _single_line(name, "name", MAX_MEMORY_NAME_CHARS)
    description = _single_line(
        description, "description", MAX_MEMORY_DESCRIPTION_CHARS, allow_empty=True
    )
    memory_type = parse_semantic_memory_type(type_)
    if memory_type is None:
        raise SemanticMemoryStoreError(f"非法 memory type: {type_}")
    content = _content(body)
    created_at = created_at or _utc_now()
    updated_at = updated_at or created_at
    provenance = ""
    if origin or source_refs:
        origin_line = _single_line(origin, "origin", 40)
        refs = ",".join(
            _single_line(item, "source_ref", 180) for item in source_refs
        )
        provenance = f"origin: {origin_line}\nsource_refs: {refs}\n"
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {memory_type}\n"
        f"created_at: {created_at}\n"
        f"updated_at: {updated_at}\n"
        f"{provenance}"
        "---\n\n"
        f"{content}\n"
    )


def slugify(name: str) -> str:
    """Create a Unicode-safe id; punctuation-only names use a fixed fallback."""
    normalized = unicodedata.normalize("NFKC", str(name)).strip().casefold()
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE)
    slug = slug.replace("_", "-").strip("-")[:160].strip("-")
    if slug:
        return slug
    return "memory"


def normalize_memory_id(memory_id: str) -> str:
    value = str(memory_id).strip()
    value = value.removesuffix(".md")
    if not value or _SAFE_ID_RE.fullmatch(value) is None:
        raise SemanticMemoryStoreError("memory_id 必须是安全的记忆 id，不能包含路径")
    return value


def _memory_path(memory_id: str, directory: Path) -> Path:
    normalized = normalize_memory_id(memory_id)
    path = (directory / f"{normalized}.md").resolve()
    if path.parent != directory:
        raise SemanticMemoryStoreError("memory path 越界")
    return path


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


def scan_memory_files(directory: Path | None = None) -> list[SemanticMemoryHeader]:
    directory = _directory(directory)
    if not directory.is_dir():
        return []

    headers: list[SemanticMemoryHeader] = []
    for path in directory.iterdir():
        if (
            path.name == MEMORY_INDEX
            or path.suffix != ".md"
            or not path.is_file()
            or path.is_symlink()
        ):
            continue
        try:
            head = _read_head(path, FRONTMATTER_MAX_LINES)
            fm, _ = parse_frontmatter(head)
            memory_id = path.stem
            headers.append(SemanticMemoryHeader(
                id=memory_id,
                filename=path.name,
                path=path,
                mtime=path.stat().st_mtime,
                name=fm.get("name") or memory_id,
                description=fm.get("description") or None,
                type=parse_semantic_memory_type(fm.get("type")),
                created_at=fm.get("created_at"),
                updated_at=fm.get("updated_at"),
            ))
        except OSError:
            continue
    headers.sort(key=lambda header: header.mtime, reverse=True)
    return headers[:MAX_MEMORY_FILES]


def format_manifest(headers: list[SemanticMemoryHeader]) -> str:
    lines: list[str] = []
    for header in headers:
        tag = f"[{header.type}] " if header.type else ""
        desc = f": {header.description}" if header.description else ""
        lines.append(f"- {tag}{header.filename}{desc}")
    return "\n".join(lines)


def get_memory(memory_id: str, directory: Path | None = None) -> SemanticMemoryRecord:
    directory = _directory(directory)
    path = _memory_path(memory_id, directory)
    try:
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
    except OSError as exc:
        raise SemanticMemoryNotFoundError(f"记忆不存在: {normalize_memory_id(memory_id)}") from exc
    fm, body = parse_frontmatter(text)
    memory_type = parse_semantic_memory_type(fm.get("type"))
    if memory_type is None:
        raise SemanticMemoryStoreError(f"记忆 {path.name} 缺少合法 type")
    fallback_time = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    return SemanticMemoryRecord(
        id=path.stem,
        name=fm.get("name") or path.stem,
        description=fm.get("description") or "",
        type=memory_type,
        content=body.strip(),
        created_at=fm.get("created_at") or fallback_time,
        updated_at=fm.get("updated_at") or fallback_time,
        path=path,
        origin=fm.get("origin") or "",
        source_refs=_source_refs(fm.get("source_refs", "")),
    )


def _source_refs(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def create_memory(
    name: str,
    description: str,
    type_: str,
    content: str,
    directory: Path | None = None,
    *,
    origin: str = "",
    source_refs: tuple[str, ...] = (),
) -> SemanticMemoryRecord:
    directory = _directory(directory)
    memory_id = slugify(_single_line(name, "name", MAX_MEMORY_NAME_CHARS))
    path = _memory_path(memory_id, directory)
    with _lock_for(directory):
        if path.exists():
            raise SemanticMemoryAlreadyExistsError(
                f"记忆已存在: {memory_id}; 请使用 update_memory"
            )
        now = _utc_now()
        _atomic_write(
            path,
            dump_frontmatter(
                name,
                description,
                type_,
                content,
                created_at=now,
                updated_at=now,
                origin=origin,
                source_refs=source_refs,
            ),
        )
        _rebuild_index_unlocked(directory)
    return get_memory(memory_id, directory)


_UNSET = object()


def update_memory(
    memory_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    type_: str | None = None,
    content: str | None = None,
    directory: Path | None = None,
    origin: str | object = _UNSET,
    source_refs: tuple[str, ...] | object = _UNSET,
) -> SemanticMemoryRecord:
    directory = _directory(directory)
    normalized = normalize_memory_id(memory_id)
    with _lock_for(directory):
        current = get_memory(normalized, directory)
        updated_name = current.name if name is None else name
        if content is not None and origin is _UNSET and source_refs is _UNSET:
            # A new body cannot keep citations that described the previous text.
            next_origin = ""
            next_refs: tuple[str, ...] = ()
        else:
            next_origin = current.origin if origin is _UNSET else str(origin)
            next_refs = (
                current.source_refs
                if source_refs is _UNSET
                else tuple(source_refs)  # type: ignore[arg-type]
            )
        text = dump_frontmatter(
            updated_name,
            current.description if description is None else description,
            current.type if type_ is None else type_,
            current.content if content is None else content,
            created_at=current.created_at,
            updated_at=_utc_now(),
            origin=next_origin,
            source_refs=next_refs,
        )
        current_path = _memory_path(normalized, directory)
        _atomic_write(current_path, text)
        _rebuild_index_unlocked(directory)
    return get_memory(normalized, directory)


def delete_memory(memory_id: str, directory: Path | None = None) -> SemanticMemoryRecord:
    directory = _directory(directory)
    normalized = normalize_memory_id(memory_id)
    with _lock_for(directory):
        current = get_memory(normalized, directory)
        current_path = _memory_path(normalized, directory)
        try:
            current_path.unlink()
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
) -> list[SemanticMemoryRecord]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise SemanticMemoryStoreError("limit 必须是 1..100 的整数")
    if type_ is not None and parse_semantic_memory_type(type_) is None:
        raise SemanticMemoryStoreError(f"非法 memory type: {type_}")
    query_text = str(query).strip().casefold()
    terms = [term for term in re.split(r"\s+", query_text) if term]
    scored: list[tuple[int, float, SemanticMemoryRecord]] = []
    for header in scan_memory_files(directory):
        if type_ is not None and header.type != type_:
            continue
        try:
            record = get_memory(header.id, directory)
        except SemanticMemoryStoreError:
            continue
        haystack = f"{record.id}\n{record.name}\n{record.description}\n{record.type}\n{record.content}".casefold()
        if terms and not all(term in haystack for term in terms):
            continue
        score = sum(haystack.count(term) for term in terms) if terms else 0
        scored.append((score, header.mtime, record))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [record for _, _, record in scored[:limit]]


def write_memory_file(
    name: str,
    description: str,
    type_: str,
    content: str,
    directory: Path | None = None,
) -> Path:
    """Backward-compatible upsert used by automatic extraction."""
    directory = _directory(directory)
    memory_id = slugify(_single_line(name, "name", MAX_MEMORY_NAME_CHARS))
    path = _memory_path(memory_id, directory)
    with _lock_for(directory):
        if path.exists():
            current = get_memory(memory_id, directory)
            created_at = current.created_at
        else:
            created_at = _utc_now()
        _atomic_write(
            path,
            dump_frontmatter(
                name,
                description,
                type_,
                content,
                created_at=created_at,
                updated_at=_utc_now(),
            ),
        )
    return path


def rebuild_index(directory: Path | None = None) -> Path:
    directory = _directory(directory)
    with _lock_for(directory):
        return _rebuild_index_unlocked(directory)


def _rebuild_index_unlocked(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    headers = scan_memory_files(directory)
    lines = ["# MEMORY.md", "", "记忆索引(每行一条指针,正文在各自文件里)。", ""]
    for header in headers:
        desc = f" — {header.description}" if header.description else ""
        tag = f" `{header.type}`" if header.type else ""
        lines.append(
            f"- [{header.name}]({header.filename}){desc}{tag}"
        )
    if not headers:
        lines.append("_(暂无记忆)_")
    index_path = directory / MEMORY_INDEX
    _atomic_write(index_path, "\n".join(lines) + "\n")
    return index_path


def read_entrypoint(directory: Path | None = None) -> str:
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
    """Markdown semantic memories. The id is the slug of the name."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = _directory(directory)

    def read_index(self) -> str:
        return read_entrypoint(self.directory)

    def list(self, limit: int = 100) -> list[SemanticMemoryRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise SemanticMemoryStoreError("limit 必须是 1..200 的整数")
        records: list[SemanticMemoryRecord] = []
        for header in scan_memory_files(self.directory):
            try:
                records.append(get_memory(header.id, self.directory))
            except SemanticMemoryStoreError:
                continue
            if len(records) >= limit:
                break
        return records

    def get(self, memory_id: str) -> SemanticMemoryRecord:
        return get_memory(memory_id, self.directory)

    def search(self, query: str = "", *, limit: int = 20) -> list[SemanticMemoryRecord]:
        return search_memories(query, limit=limit, directory=self.directory)

    def save(
        self,
        *,
        name: str,
        description: str,
        type_: SemanticMemoryType,
        content: str,
        origin: str = "",
        source_refs: tuple[str, ...] = (),
        memory_id: str = "",
    ) -> SemanticMemoryRecord:
        target = memory_id or slugify(name)
        try:
            get_memory(target, self.directory)
        except SemanticMemoryNotFoundError:
            if memory_id:
                raise
            return create_memory(
                name,
                description,
                type_,
                content,
                self.directory,
                origin=origin,
                source_refs=source_refs,
            )
        return update_memory(
            target,
            name=name,
            description=description,
            type_=type_,
            content=content,
            directory=self.directory,
            origin=origin,
            source_refs=source_refs,
        )

    def delete(self, memory_id: str) -> SemanticMemoryRecord:
        return delete_memory(memory_id, directory=self.directory)


__all__ = [
    "FRONTMATTER_MAX_LINES",
    "MAX_INDEX_BYTES",
    "MAX_INDEX_LINES",
    "MAX_MEMORY_CHARS",
    "MAX_MEMORY_CONTENT_CHARS",
    "MAX_MEMORY_DESCRIPTION_CHARS",
    "MAX_MEMORY_FILES",
    "MAX_MEMORY_NAME_CHARS",
    "SemanticMemoryStore",
    "SemanticMemoryStoreError",
    "create_memory",
    "delete_memory",
    "dump_frontmatter",
    "format_manifest",
    "get_memory",
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

