"""Text splitter and chunking utilities for context windowing and RAG."""

from __future__ import annotations

import re


def split_text(
    text: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    separators: list[str] | None = None,
) -> list[str]:
    """Split long text into chunks of bounded size with overlapping boundaries."""
    if not text:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be less than chunk_size")

    if len(text) <= chunk_size:
        return [text]

    separators = separators or ["\n\n", "\n", "。", ".", " ", ""]

    def _split_recursive(t: str, seps: list[str]) -> list[str]:
        if len(t) <= chunk_size or not seps:
            return [t] if t else []

        sep = seps[0]
        remaining_seps = seps[1:]

        if sep == "":
            # Character-level chunking
            chunks: list[str] = []
            for i in range(0, len(t), chunk_size - chunk_overlap):
                chunks.append(t[i : i + chunk_size])
            return chunks

        parts = t.split(sep)
        splits: list[str] = []
        for part in parts:
            if not part:
                continue
            if len(part) <= chunk_size:
                splits.append(part)
            else:
                splits.extend(_split_recursive(part, remaining_seps))
        return splits

    raw_splits = _split_recursive(text, separators)

    # Merge splits into chunks up to chunk_size with overlap
    merged_chunks: list[str] = []
    current_chunk: list[str] = []
    current_length = 0

    for split in raw_splits:
        split_len = len(split)
        if current_length + split_len + 1 > chunk_size and current_chunk:
            merged_chunks.append("\n".join(current_chunk))
            # Keep overlap items from the tail
            overlap_items: list[str] = []
            overlap_len = 0
            for item in reversed(current_chunk):
                if overlap_len + len(item) <= chunk_overlap:
                    overlap_items.insert(0, item)
                    overlap_len += len(item)
                else:
                    break
            current_chunk = overlap_items
            current_length = sum(len(item) for item in current_chunk)

        current_chunk.append(split)
        current_length += split_len

    if current_chunk:
        merged_chunks.append("\n".join(current_chunk))

    return merged_chunks
