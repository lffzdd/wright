"""read tool implementation."""

from __future__ import annotations

import hashlib
import re
from io import BytesIO
from pathlib import PurePath

from pypdf import PdfReader
from pypdf.errors import PdfReadError as PdfError

from ....domain.model.tool import ToolResult
from ....domain.model.tool_definition import Tool
from ....domain.model.tool_runtime import ToolRuntime
from ...storage.attachments import MAX_ATTACHMENT_BYTES, AttachmentError, inspect_image
from .common import (
    FILE_UNCHANGED,
    MAX_READ_CHARS,
    _backend,
    _describe_file_read,
    _detect_encoding,
    _numbered_content,
    _remember_file_view,
    _remembered_file_view,
    _safe_path,
    _stamp_read_view,
    _unchanged_read_view,
)

_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
MAX_PDF_PAGES = 50


def _is_image(head: bytes) -> bool:
    return head.startswith(_IMAGE_MAGIC) or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")


def _read_image(safe_path, runtime: ToolRuntime) -> ToolResult:
    """Register the image so the next model request shows it to the model."""
    capabilities = runtime.capabilities
    artifacts = capabilities.artifacts if capabilities is not None else None
    if artifacts is None:
        return ToolResult.fail("Image viewing is not available in this session", data={"content": ""})
    size = _backend(runtime).metadata(safe_path).size
    if size > MAX_ATTACHMENT_BYTES:
        return ToolResult.fail("Image is larger than 20 MiB", data={"content": ""})
    data = _backend(runtime).read_bytes(safe_path, MAX_ATTACHMENT_BYTES + 1)
    try:
        media_type, _suffix, width, height = inspect_image(BytesIO(data))
    except AttachmentError as exc:
        return ToolResult.fail(str(exc), data={"content": ""})
    ref = artifacts.register(
        data,
        name=PurePath(str(safe_path.value)).name or "image",
        media_type=media_type,
        run_id=capabilities.scope.run_id,
        call_id=runtime.tool_call_id,
    )
    return ToolResult.success(
        {"type": "image", "media_type": media_type, "width": width, "height": height, "size": len(data)},
        summary=f"image {width}x{height}",
        artifacts=(ref,),
    )


def _page_range(pages: str | None, count: int) -> range:
    if not pages:
        return range(min(count, MAX_PDF_PAGES))
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", str(pages))
    if match is None:
        raise ValueError('pages must look like "3" or "1-5"')
    first = int(match.group(1))
    last = int(match.group(2) or first)
    if first < 1 or last < first:
        raise ValueError("pages must be a valid 1-based range")
    if last - first + 1 > MAX_PDF_PAGES:
        raise ValueError(f"read at most {MAX_PDF_PAGES} pages at a time")
    return range(first - 1, min(last, count))


def _read_pdf(safe_path, runtime: ToolRuntime, pages: str | None, max_chars: int) -> ToolResult:
    size = _backend(runtime).metadata(safe_path).size
    if size > MAX_ATTACHMENT_BYTES:
        return ToolResult.fail("PDF is larger than 20 MiB", data={"content": ""})
    try:
        reader = PdfReader(BytesIO(_backend(runtime).read_bytes(safe_path, MAX_ATTACHMENT_BYTES + 1)))
        if reader.is_encrypted:
            return ToolResult.fail("PDF is encrypted", data={"content": ""})
        selected = _page_range(pages, len(reader.pages))
        chunks = []
        for index in selected:
            runtime.raise_if_cancelled()
            chunks.append(f"--- page {index + 1} ---\n{(reader.pages[index].extract_text() or '').strip()}")
    except PdfError as exc:
        return ToolResult.fail(f"PDF cannot be read: {exc}", data={"content": ""})
    text = "\n\n".join(chunks)
    truncated = len(text) > max_chars
    data = {
        "type": "pdf",
        "content": text[:max_chars],
        "page_count": len(reader.pages),
        "pages": f"{selected.start + 1}-{selected.stop}" if len(selected) else "",
        "truncated": truncated,
    }
    if selected.stop < len(reader.pages):
        data["next_pages"] = f"{selected.stop + 1}-{min(selected.stop + len(selected), len(reader.pages))}"
    return ToolResult.success(data)


def read_file(
    file: str,
    start_line: int = 1,
    start_column: int = 1,
    end_line: int | None = None,
    max_chars: int = 8000,
    pages: str | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolResult:
    try:
        assert runtime is not None
        runtime.raise_if_cancelled()
        safe_path = _safe_path(file, runtime)

        if _backend(runtime).metadata(safe_path).kind != "file":
            return ToolResult.fail("Not a file", data={"content": ""})

        try:
            max_chars = int(max_chars)
        except (ValueError, TypeError):
            max_chars = 8000
        max_chars = max(0, min(max_chars, MAX_READ_CHARS))

        head = _backend(runtime).read_bytes(safe_path, 16)
        if _is_image(head):
            return _read_image(safe_path, runtime)
        if head.startswith(b"%PDF-"):
            return _read_pdf(safe_path, runtime, pages, max_chars)

        start_line = max(1, int(start_line))
        start_column = max(1, int(start_column))
        if end_line is not None:
            end_line = int(end_line)
            if end_line < start_line:
                return ToolResult.fail("end_line must be greater than or equal to start_line")

        viewed = _remembered_file_view(runtime, safe_path)
        stat = _backend(runtime).metadata(safe_path)
        encoding = _detect_encoding(safe_path, runtime)
        source_content = _backend(runtime).read_text(safe_path, encoding=encoding, errors="replace")
        digest = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
        if (viewed is not None and viewed.digest == digest) and _unchanged_read_view(
            viewed,
            mtime_ns=stat.modified_ns,
            size=stat.size,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            max_chars=max_chars,
        ):
            assert viewed is not None
            _remember_file_view(runtime, safe_path, viewed)
            return ToolResult.success({
                "content": FILE_UNCHANGED,
                "unchanged": True,
                "start_line": start_line,
                "start_column": start_column,
                "end_line": viewed.last_line,
                "next_start_line": viewed.next_start_line,
                "next_start_column": viewed.next_start_column,
                "truncated": viewed.truncated,
            })

        selected: list[str] = []
        total_chars = 0
        truncated = False
        last_line = start_line - 1
        next_start_line: int | None = None
        next_start_column: int | None = None
        source_lines = source_content.splitlines(keepends=True)
        for line_number, line in enumerate(source_lines, 1):
            runtime.raise_if_cancelled()
            if line_number < start_line:
                continue
            if end_line is not None and line_number > end_line:
                break
            visible_line = line[start_column - 1:] if line_number == start_line else line
            remaining = max_chars - total_chars
            if len(visible_line) > remaining:
                selected.append(visible_line[:remaining])
                total_chars = max_chars
                last_line = line_number
                truncated = True
                consumed_column = (
                    start_column + remaining
                    if line_number == start_line
                    else 1 + remaining
                )
                next_start_line = line_number
                next_start_column = consumed_column
                break
            selected.append(visible_line)
            total_chars += len(visible_line)
            last_line = line_number
        content = "".join(selected)
        _stamp_read_view(
            safe_path,
            runtime,
            content=content,
            source_content=source_content,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            max_chars=max_chars,
            truncated=truncated,
            last_line=last_line,
            next_start_line=next_start_line,
            next_start_column=next_start_column,
        )
        return ToolResult.success({
            "content": _numbered_content(content, start_line),
            "start_line": start_line,
            "start_column": start_column,
            "end_line": last_line,
            "next_start_line": next_start_line,
            "next_start_column": next_start_column,
            "truncated": truncated,
        })
    except Exception as e:
        return ToolResult.fail(str(e), data={"content": ""})


read_file_tool = Tool(
    name="read",
    description=(
        "Read a workspace file. Each returned line is prefixed with N| for reference; "
        "do not copy those prefixes into edit. An identical unchanged re-read "
        "returns a short stub. PNG, JPEG and WebP images are shown to you as images. "
        "PDFs return extracted text per page; use pages (e.g. \"1-5\") for long documents."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Path relative to the session's current working directory",
            },
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "start_column": {"type": "integer", "minimum": 1, "default": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "max_chars": {"type": "integer", "minimum": 0, "maximum": MAX_READ_CHARS, "default": 8000},
            "pages": {"type": "string", "description": "PDF only: 1-based page or range, e.g. \"3\" or \"1-5\""},
        },
        "required": ["file"],
    },
    call=lambda args, runtime: read_file(**args, runtime=runtime),
    access_descriptor=_describe_file_read,
    is_concurrency_safe=lambda args: True,
    required_capabilities=frozenset({"execution", "artifacts"}),
)
