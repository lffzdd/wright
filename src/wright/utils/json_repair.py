"""Fault-tolerant JSON parser that repairs malformed or truncated LLM outputs."""

from __future__ import annotations

import json
import re
from typing import Any

_MARKDOWN_CODE_BLOCK_RE = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE
)
_TRAILING_COMMA_RE = re.compile(r",\s*([\]}])")


def repair_json(text: str) -> str:
    """Repair truncated or malformed JSON text into valid parseable JSON."""
    s = text.strip()
    if not s:
        return "{}"

    # 1. Strip markdown code fences if present
    match = _MARKDOWN_CODE_BLOCK_RE.match(s)
    if match:
        s = match.group(1).strip()

    # If it already parses cleanly, return as-is
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass

    # 2. Extract first valid JSON object or array structure if surrounded by noise
    first_brace = s.find("{")
    first_bracket = s.find("[")
    start = -1
    if first_brace != -1 and first_bracket != -1:
        start = min(first_brace, first_bracket)
    elif first_brace != -1:
        start = first_brace
    elif first_bracket != -1:
        start = first_bracket

    if start > 0:
        s = s[start:]

    # 3. Strip trailing commas before closing braces/brackets
    s = _TRAILING_COMMA_RE.sub(r"\1", s)

    # Try again
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass

    # 4. Handle unclosed quotes and balance open brackets/braces
    stack: list[str] = []
    in_string = False
    escape = False
    cleaned_chars: list[str] = []

    for char in s:
        if in_string:
            if escape:
                escape = False
                cleaned_chars.append(char)
            elif char == "\\":
                escape = True
                cleaned_chars.append(char)
            elif char == '"':
                in_string = False
                cleaned_chars.append(char)
            else:
                cleaned_chars.append(char)
        else:
            if char == '"':
                in_string = True
                cleaned_chars.append(char)
            elif char in ("{", "["):
                stack.append(char)
                cleaned_chars.append(char)
            elif char == "}":
                if stack and stack[-1] == "{":
                    stack.pop()
                cleaned_chars.append(char)
            elif char == "]":
                if stack and stack[-1] == "[":
                    stack.pop()
                cleaned_chars.append(char)
            else:
                cleaned_chars.append(char)

    # If string was left open at EOF, close it
    if in_string:
        cleaned_chars.append('"')

    # Close any unclosed braces/brackets in reverse order
    while stack:
        opener = stack.pop()
        cleaned_chars.append("}" if opener == "{" else "]")

    repaired = "".join(cleaned_chars)
    # Strip any trailing commas that may now precede closing braces
    repaired = _TRAILING_COMMA_RE.sub(r"\1", repaired)
    return repaired


def loads_repaired_json(text: str) -> Any:
    """Parse JSON string with automatic repair fallback."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        repaired = repair_json(text)
        return json.loads(repaired)
