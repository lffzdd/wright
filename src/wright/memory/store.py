"""File-backed semantic memory CRUD with atomic index maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata

from .paths import MEMORY_INDEX, entrypoint_path, memory_dir
from .types import MemoryType, parse_memory_type

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
FRONTMATTER_MAX_LINES = 30
MAX_MEMORY_CHARS = 4_000
MAX_MEMORY_FILES = 200
MAX_MEMORY_NAME_CHARS = 120
MAX_MEMORY_DESCRIPTION_CHARS = 500
MAX_MEMORY_CONTENT_CHARS = 12_000


class MemoryStoreError(ValueError):
    """Semantic memory input or state is invalid."""


class MemoryAlreadyExistsError(MemoryStoreError):
    pass


class MemoryNotFoundError(MemoryStoreError):
    pass


@dataclass(frozen=True)
class MemoryHeader:
    id: str
    filename: str
    path: Path
    mtime: float
    name: str
    description: str | None
    type: MemoryType | None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    name: str
    description: str
    type: MemoryType
    content: str
    created_at: str
    updated_at: str
    path: Path

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "file": self.path.name,
        }


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
) -> str:
    name = _single_line(name, "name", MAX_MEMORY_NAME_CHARS)
    description = _single_line(
        description, "description", MAX_MEMORY_DESCRIPTION_CHARS, allow_empty=True
    )
    memory_type = parse_memory_type(type_)
    if memory_type is None:
        raise MemoryStoreError(f"非法 memory type: {type_}")
    content = _content(body)
    created_at = created_at or _utc_now()
    updated_at = updated_at or created_at
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {memory_type}\n"
        f"created_at: {created_at}\n"
        f"updated_at: {updated_at}\n"
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
    if value.endswith(".md"):
        value = value[:-3]
    if not value or _SAFE_ID_RE.fullmatch(value) is None:
        raise MemoryStoreError("memory_id 必须是安全的记忆 id，不能包含路径")
    return value


def _memory_path(memory_id: str, directory: Path) -> Path:
    normalized = normalize_memory_id(memory_id)
    path = (directory / f"{normalized}.md").resolve()
    if path.parent != directory:
        raise MemoryStoreError("memory path 越界")
    return path


def _single_line(
    value: object,
    field: str,
    max_chars: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise MemoryStoreError(f"{field} 必须是字符串")
    cleaned = " ".join(value.splitlines()).strip()
    if not allow_empty and not cleaned:
        raise MemoryStoreError(f"{field} 不能为空")
    if len(cleaned) > max_chars:
        raise MemoryStoreError(f"{field} 不能超过 {max_chars} 个字符")
    return cleaned


def _content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryStoreError("content 不能为空")
    cleaned = value.strip()
    if len(cleaned) > MAX_MEMORY_CONTENT_CHARS:
        raise MemoryStoreError(
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
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index >= max_lines:
                break
            lines.append(line)
    return "".join(lines)


def scan_memory_files(directory: Path | None = None) -> list[MemoryHeader]:
    directory = _directory(directory)
    if not directory.is_dir():
        return []

    headers: list[MemoryHeader] = []
    # Semantic memories are deliberately flat.  This keeps ids unambiguous and
    # prevents the episodes/ area from accidentally entering semantic recall.
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
            headers.append(MemoryHeader(
                id=memory_id,
                filename=path.name,
                path=path,
                mtime=path.stat().st_mtime,
                name=fm.get("name") or memory_id,
                description=fm.get("description") or None,
                type=parse_memory_type(fm.get("type")),
                created_at=fm.get("created_at"),
                updated_at=fm.get("updated_at"),
            ))
        except OSError:
            continue
    headers.sort(key=lambda header: header.mtime, reverse=True)
    return headers[:MAX_MEMORY_FILES]


def format_manifest(headers: list[MemoryHeader]) -> str:
    lines: list[str] = []
    for header in headers:
        tag = f"[{header.type}] " if header.type else ""
        desc = f": {header.description}" if header.description else ""
        lines.append(f"- {tag}{header.filename}{desc}")
    return "\n".join(lines)


def get_memory(memory_id: str, directory: Path | None = None) -> MemoryRecord:
    directory = _directory(directory)
    path = _memory_path(memory_id, directory)
    try:
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
    except OSError as exc:
        raise MemoryNotFoundError(f"记忆不存在: {normalize_memory_id(memory_id)}") from exc
    fm, body = parse_frontmatter(text)
    memory_type = parse_memory_type(fm.get("type"))
    if memory_type is None:
        raise MemoryStoreError(f"记忆 {path.name} 缺少合法 type")
    fallback_time = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    return MemoryRecord(
        id=path.stem,
        name=fm.get("name") or path.stem,
        description=fm.get("description") or "",
        type=memory_type,
        content=body.strip(),
        created_at=fm.get("created_at") or fallback_time,
        updated_at=fm.get("updated_at") or fallback_time,
        path=path,
    )


def create_memory(
    name: str,
    description: str,
    type_: str,
    content: str,
    directory: Path | None = None,
) -> MemoryRecord:
    directory = _directory(directory)
    memory_id = slugify(_single_line(name, "name", MAX_MEMORY_NAME_CHARS))
    path = _memory_path(memory_id, directory)
    with _lock_for(directory):
        if path.exists():
            raise MemoryAlreadyExistsError(
                f"记忆已存在: {memory_id}; 请使用 update_memory"
            )
        now = _utc_now()
        _atomic_write(
            path,
            dump_frontmatter(
                name, description, type_, content, created_at=now, updated_at=now
            ),
        )
        _rebuild_index_unlocked(directory)
    return get_memory(memory_id, directory)


def update_memory(
    memory_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    type_: str | None = None,
    content: str | None = None,
    directory: Path | None = None,
) -> MemoryRecord:
    directory = _directory(directory)
    normalized = normalize_memory_id(memory_id)
    with _lock_for(directory):
        current = get_memory(normalized, directory)
        updated_name = current.name if name is None else name
        # IDs are stable across rename: references and manifests do not break.
        text = dump_frontmatter(
            updated_name,
            current.description if description is None else description,
            current.type if type_ is None else type_,
            current.content if content is None else content,
            created_at=current.created_at,
            updated_at=_utc_now(),
        )
        _atomic_write(current.path, text)
        _rebuild_index_unlocked(directory)
    return get_memory(normalized, directory)


def delete_memory(memory_id: str, directory: Path | None = None) -> MemoryRecord:
    directory = _directory(directory)
    normalized = normalize_memory_id(memory_id)
    with _lock_for(directory):
        current = get_memory(normalized, directory)
        try:
            current.path.unlink()
        except OSError as exc:
            raise MemoryStoreError(f"删除记忆失败: {exc}") from exc
        _rebuild_index_unlocked(directory)
    return current


def search_memories(
    query: str = "",
    *,
    type_: str | None = None,
    limit: int = 20,
    directory: Path | None = None,
) -> list[MemoryRecord]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise MemoryStoreError("limit 必须是 1..100 的整数")
    if type_ is not None and parse_memory_type(type_) is None:
        raise MemoryStoreError(f"非法 memory type: {type_}")
    query_text = str(query).strip().casefold()
    terms = [term for term in re.split(r"\s+", query_text) if term]
    scored: list[tuple[int, float, MemoryRecord]] = []
    for header in scan_memory_files(directory):
        if type_ is not None and header.type != type_:
            continue
        try:
            record = get_memory(header.id, directory)
        except MemoryStoreError:
            continue
        haystack = "\n".join(
            [record.id, record.name, record.description, record.type, record.content]
        ).casefold()
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
