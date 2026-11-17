"""Pure rules for the fact write pipeline: origin, sensitivity, markers, duplicates.

The model never sets ``origin`` and can only raise ``sensitivity``. These
functions take plain text and return decisions; the application layer runs
them in order and stores the result.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from ...model.memory import (
    MAX_MARKER_CHARS,
    MAX_MARKERS,
    SENSITIVITY_RANK,
    FactSubject,
    Origin,
    Sensitivity,
    SpaceType,
    Tier,
)
from .lexical import tokenize

# "Remember this" in the user's own words. Opening a write is all it does.
REMEMBER_INTENT = re.compile(
    r"记住|记一下|记下来|帮我记|别忘了|以后(?:都|不要|别|必须|只用|不再)|别再|不要再|"
    r"我习惯|我更喜欢|我偏好|我们决定|必须用|不要用|改成用|"
    r"\bremember\b|\bfrom now on\b|\bi prefer\b|\bi'd rather\b|\bkeep in mind\b|"
    r"\bwe decided\b|\bnever again\b|\bdo not use\b|\bdon't use\b|\bthe rule is\b|\bnote that\b",
    re.IGNORECASE,
)

_SENSITIVE_GROUPS = (
    # health
    (
        r"生病|病情|病史|医院|诊断|确诊|手术|住院|抑郁|焦虑|心理(?:医生|咨询|治疗)|精神科|吃药|服药|"
        r"怀孕|流产|癌|肿瘤|糖尿病|高血压|艾滋|hiv|残疾|"
        r"\btherap(?:y|ist)\b|\bdiagnos|\bdepress(?:ion|ed)\b|\banxiety\b|\bpsychiatr|\bpregnan|"
        r"\bcancer\b|\bdiabet|\bmedication\b|\bdisorder\b|\bdisabilit"
    ),
    # money
    (
        r"工资|薪资|薪水|年薪|月薪|收入|存款|负债|欠款|贷款|房贷|破产|"
        r"\bsalary\b|\bincome\b|\bdebt\b|\bmortgage\b|\bbankrupt"
    ),
    # relationships and family conflict
    r"离婚|分手|出轨|婚外|家暴|恋爱|约会|\bdivorc|\baffair\b|\bbreak ?up\b|\bdating\b",
    # beliefs and identity
    (
        r"宗教|信仰|政治(?:立场|倾向|观点)|性取向|同性恋|双性恋|跨性别|"
        r"\breligio|\bpolitical (?:view|leaning|affiliation)|\bsexual orientation\b|\bgay\b|"
        r"\blesbian\b|\bbisexual\b|\btransgender\b"
    ),
    # legal and identity documents
    (
        r"犯罪|案底|诉讼|起诉|判刑|逮捕|身份证|护照号|社保号|"
        r"\blawsuit\b|\barrest|\bcriminal record\b|\bpassport (?:number|no)\b|\bssn\b|\bsocial security\b"
    ),
)
# P2 categories. A keyword match is enough to treat a fact as sensitive.
_SENSITIVE = re.compile("|".join(_SENSITIVE_GROUPS), re.IGNORECASE)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"https?://[^\s'\"<>）)]+")
_HOST = re.compile(
    r"\b(?:[A-Za-z0-9-]+\.)+(?:internal|local|lan|corp|intra|intranet|private|"
    r"com|net|org|io|dev|cn|co|ai|app|cloud)\b"
)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PHONE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|\+?\d{1,3}[- ]\d{3,4}[- ]\d{4})(?!\d)")
_QUOTED = re.compile(r"[「“\"'‘『《]([^」”\"'’』》\n]{2,40})[」”\"'’』》]")
_HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9_-]{3,40}")

DUPLICATE_SIMILARITY = 0.85
RELATED_SIMILARITY = 0.45
_SHINGLE = 4
USER_COVERAGE = 0.35
EXTERNAL_COVERAGE = 0.5


def has_remember_intent(texts: Iterable[str]) -> bool:
    return any(isinstance(text, str) and REMEMBER_INTENT.search(text) for text in texts)


def looks_sensitive(text: str) -> bool:
    return bool(text) and _SENSITIVE.search(text) is not None


def classify_sensitivity(
    content: str,
    *,
    subject: FactSubject,
    space_type: SpaceType,
    hint: Sensitivity | None = None,
) -> Sensitivity:
    """Rules first, then the hint. The hint can raise the level, never lower it."""
    if looks_sensitive(content):
        level: Sensitivity = "sensitive"
    elif space_type in {"personal", "global"} or subject == "user":
        level = "personal"
    else:
        level = "none"
    if hint and SENSITIVITY_RANK.get(hint, 0) > SENSITIVITY_RANK[level]:
        return hint
    return level


def normalize_for_match(text: str) -> str:
    """Case-fold, unify width, and drop whitespace and punctuation."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def shingles(text: str, size: int = _SHINGLE) -> set[str]:
    norm = normalize_for_match(text)
    if not norm:
        return set()
    if len(norm) <= size:
        return {norm}
    return {norm[index:index + size] for index in range(len(norm) - size + 1)}


def coverage(content: str, texts: Sequence[str]) -> float:
    """Share of ``content`` shingles that also occur in ``texts``."""
    mine = shingles(content)
    if not mine:
        return 0.0
    theirs: set[str] = set()
    for text in texts:
        theirs |= shingles(text)
    if not theirs:
        return 0.0
    return len(mine & theirs) / len(mine)


def infer_origin(
    content: str,
    *,
    user_texts: Sequence[str],
    external_texts: Sequence[str],
) -> Origin:
    """Where an agent-written fact came from, judged by text overlap.

    Content mostly copied from a tool result is ``external`` unless the user's
    own words this turn ask to remember it, which makes it ``user_confirmed``.
    """
    user = coverage(content, user_texts)
    external = coverage(content, external_texts)
    remember = has_remember_intent(user_texts)
    if external >= EXTERNAL_COVERAGE and external > user:
        return "user_confirmed" if remember else "external"
    if remember or user >= USER_COVERAGE:
        return "user_stated"
    return "agent_inferred"


def default_tier(origin: Origin, *, pin: bool) -> Tier:
    if origin == "agent_inferred":
        return "archived"
    if pin and origin in {"user_stated", "user_confirmed"}:
        return "pinned"
    return "indexed"


def extract_markers(content: str) -> list[str]:
    """Distinctive strings that would give a fact away if they showed up elsewhere."""
    found: list[str] = []
    for pattern in (_EMAIL, _URL, _HOST, _IPV4, _PHONE, _HANDLE):
        found.extend(match.group(0) for match in pattern.finditer(content))
    found.extend(match.group(1) for match in _QUOTED.finditer(content))
    return normalize_markers(found)


def normalize_markers(values: Iterable[object]) -> list[str]:
    markers: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split()).strip(" .,;:，。；：")
        if not _marker_is_distinctive(text):
            continue
        key = normalize_for_match(text)
        if key in seen:
            continue
        seen.add(key)
        markers.append(text[:MAX_MARKER_CHARS])
        if len(markers) >= MAX_MARKERS:
            break
    return markers


def _marker_is_distinctive(text: str) -> bool:
    norm = normalize_for_match(text)
    if not norm:
        return False
    has_cjk = any("㐀" <= ch <= "鿿" for ch in norm)
    return len(norm) >= (2 if has_cjk else 3)


def similarity(left: str, right: str) -> float:
    """Token Jaccard similarity. A ranking key, not a probability."""
    a = set(tokenize(left))
    b = set(tokenize(right))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


__all__ = [
    "DUPLICATE_SIMILARITY",
    "EXTERNAL_COVERAGE",
    "RELATED_SIMILARITY",
    "REMEMBER_INTENT",
    "USER_COVERAGE",
    "classify_sensitivity",
    "coverage",
    "default_tier",
    "extract_markers",
    "has_remember_intent",
    "infer_origin",
    "looks_sensitive",
    "normalize_for_match",
    "normalize_markers",
    "shingles",
    "similarity",
]
