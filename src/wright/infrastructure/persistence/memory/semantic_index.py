"""Index and query over semantic memory files.

``scan_memory_files`` does not take the store lock. ``_rebuild_index_unlocked``
does not take it either: ``rebuild_index`` and the writers call it while they
already hold ``_StoreLock``.
"""

from __future__ import annotations

from pathlib import Path

from ....domain.model.memory import (
    SemanticMemoryHeader,
    SemanticMemoryNotFoundError,
    SemanticMemoryRecord,
    SemanticMemoryScopeError,
    SemanticMemoryStoreError,
    parse_semantic_memory_type,
)
from ....domain.policy.memory import record_in_read_scope, scope_denial_message
from .paths import MEMORY_INDEX, entrypoint_path, memory_dir
from .semantic_document import (
    FRONTMATTER_MAX_LINES,
    MAX_INDEX_BYTES,
    MAX_INDEX_LINES,
    MAX_MEMORY_CHARS,
    _atomic_write,
    _current_fields,
    normalize_memory_id,
    parse_frontmatter,
)


def _directory(directory: Path | None) -> Path:
    return (directory or memory_dir()).expanduser().resolve()


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

def _rebuild_index_unlocked(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    headers = scan_memory_files(directory)
    lines = [
        "# MEMORY.md",
        "",
        "A human index of the library. This is not the agent's recall source.",
        "Automatic context uses only global memories and status=active memories in the current project.",
        "Markdown that is not in the current format does not appear here.",
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
        lines.append("_(no memories)_")
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
            f"\n\n> Warning: {MEMORY_INDEX} exceeded its limit and was only partly loaded. "
            "Keep each index entry on one line and move detail into the memory files."
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
            text = text[:MAX_MEMORY_CHARS] + "\n…(truncated)"
        blocks.append(f"### {path.name}\n{text}")
    return "\n\n".join(blocks)
