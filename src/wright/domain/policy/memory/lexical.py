"""Dependency-free tokenizer and BM25 ranking for episode search.

Scores are rank keys. They are not similarity probabilities.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

# Fixed initial BM25 parameters. Not claimed to be optimal.
BM25_K1 = 1.2
BM25_B = 0.75

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_TOKEN = re.compile(r"[\w]+", re.UNICODE)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def tokenize(text: str) -> list[str]:
    """Normalize, split identifiers, and emit Chinese unigrams plus bigrams.

    Matching does not require every query token to occur. Callers rank with BM25.
    """
    if not text:
        return []
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _CAMEL_BOUNDARY.sub(r"\1 \2", normalized)
    normalized = _ACRONYM_BOUNDARY.sub(r"\1 \2", normalized)
    folded = normalized.casefold()
    tokens: list[str] = []
    for match in _TOKEN.finditer(folded):
        _emit(match.group(0), tokens)
    return tokens


def _emit(piece: str, tokens: list[str]) -> None:
    buffer: list[str] = []
    kind: str | None = None

    def flush(buf: list[str], current: str | None) -> None:
        if not buf or current is None:
            return
        text = "".join(buf)
        if current == "cjk":
            tokens.extend(text)
            if len(text) >= 2:
                tokens.extend(text[index:index + 2] for index in range(len(text) - 1))
            return
        for part in re.split(r"[_\-./]+", text):
            if part:
                tokens.append(part)

    for ch in piece:
        current = "cjk" if _is_cjk(ch) else "word"
        if kind is None:
            kind = current
        if current != kind:
            flush(buffer, kind)
            buffer = [ch]
            kind = current
        else:
            buffer.append(ch)
    flush(buffer, kind)


def bm25_scores(
    documents: list[list[str]],
    query: list[str],
    *,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> list[float]:
    """Okapi BM25. An empty query or empty corpus yields zeros."""
    if not documents or not query:
        return [0.0] * len(documents)
    document_count = len(documents)
    frequencies: list[Counter[str]] = []
    lengths: list[int] = []
    document_frequency: Counter[str] = Counter()
    for document in documents:
        counts = Counter(document)
        frequencies.append(counts)
        lengths.append(len(document) or 1)
        document_frequency.update(counts.keys())
    average_length = sum(lengths) / document_count
    query_terms = list(dict.fromkeys(query))
    scores: list[float] = []
    for counts, length in zip(frequencies, lengths, strict=True):
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if frequency <= 0:
                continue
            df = document_frequency[term]
            idf = math.log((document_count - df + 0.5) / (df + 0.5) + 1.0)
            denominator = frequency + k1 * (1.0 - b + b * length / average_length)
            score += idf * (frequency * (k1 + 1.0)) / denominator
        scores.append(score)
    return scores


__all__ = ["BM25_B", "BM25_K1", "bm25_scores", "tokenize"]
