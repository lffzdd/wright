"""Domain models for semantic memory: durable facts not tied to one episode.

``type`` is what the note is about. ``scope`` is where it applies. Neither
field implies the other. A stable ``id`` is not derived from the display name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SemanticMemoryType = Literal["user", "feedback", "project", "reference"]
SEMANTIC_MEMORY_TYPES: tuple[SemanticMemoryType, ...] = ("user", "feedback", "project", "reference")
SemanticScope = Literal["global", "project"]
SEMANTIC_SCOPES: tuple[SemanticScope, ...] = ("global", "project")
SemanticStatus = Literal["active", "inactive"]
SEMANTIC_STATUSES: tuple[SemanticStatus, ...] = ("active", "inactive")
SemanticReadScope = Literal["applicable", "current_project", "global", "all_projects"]
SEMANTIC_READ_SCOPES: tuple[SemanticReadScope, ...] = (
    "applicable",
    "current_project",
    "global",
    "all_projects",
)
SEMANTIC_SCHEMA_VERSION = 1
EXPLICIT_ORIGIN = "explicit"


def parse_semantic_memory_type(raw: object) -> SemanticMemoryType | None:
    """把 frontmatter 里的原始 type 值归一成合法 SemanticMemoryType。"""
    if isinstance(raw, str) and raw in SEMANTIC_MEMORY_TYPES:
        return raw  # type: ignore[return-value]
    return None


def parse_semantic_scope(raw: object) -> SemanticScope | None:
    if isinstance(raw, str) and raw in SEMANTIC_SCOPES:
        return raw  # type: ignore[return-value]
    return None


def parse_semantic_status(raw: object) -> SemanticStatus | None:
    if isinstance(raw, str) and raw in SEMANTIC_STATUSES:
        return raw  # type: ignore[return-value]
    return None


def parse_semantic_read_scope(raw: object) -> SemanticReadScope | None:
    if isinstance(raw, str) and raw in SEMANTIC_READ_SCOPES:
        return raw  # type: ignore[return-value]
    return None


class SemanticMemoryStoreError(ValueError):
    """Semantic memory input or state is invalid."""


class SemanticMemoryAlreadyExistsError(SemanticMemoryStoreError):
    pass


class SemanticMemoryNotFoundError(SemanticMemoryStoreError):
    pass


class SemanticMemoryConflictError(SemanticMemoryStoreError):
    """The record changed after the caller read it. Do not overwrite."""

    def __init__(self, message: str, *, current_revision: int) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class SemanticMemoryScopeError(SemanticMemoryStoreError):
    """The record exists, but this operation's scope does not include it."""


@dataclass(frozen=True)
class SourceLocator:
    """Persistent pointer back to one recorded source.

    A bare local id such as ``ev-u-msg_1`` is not a stored ref: the same id can
    exist in another session. Callers map a short id onto this locator before
    writing. Parsing never accepts a token that has no session.
    """

    session_id: str = ""
    root_run_id: str = ""
    run_id: str = ""
    kind: str = ""
    message_id: str = ""
    tool_call_id: str = ""
    step_id: str = ""
    episode_id: str = ""
    local_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "root_run_id": self.root_run_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "message_id": self.message_id,
            "tool_call_id": self.tool_call_id,
            "step_id": self.step_id,
            "episode_id": self.episode_id,
            "local_id": self.local_id,
        }


def encode_locator(locator: SourceLocator) -> str:
    """Stable single-line form. Values cannot contain separators."""
    if not locator.session_id:
        raise SemanticMemoryStoreError("source_ref 必须包含 session")
    fields = (
        ("sess", locator.session_id),
        ("root", locator.root_run_id),
        ("run", locator.run_id),
        ("kind", locator.kind),
        ("msg", locator.message_id),
        ("tool", locator.tool_call_id),
        ("step", locator.step_id),
        ("ep", locator.episode_id),
        ("local", locator.local_id),
    )
    parts: list[str] = []
    for key, value in fields:
        if not value:
            continue
        if any(char in value for char in ";=\n"):
            raise SemanticMemoryStoreError("source_ref 含有非法分隔符")
        parts.append(f"{key}={value}")
    return ";".join(parts)


def parse_locator(token: str) -> SourceLocator:
    """Parse one stored ref. A token without a session is rejected."""
    text = token.strip()
    if not text or "=" not in text:
        raise SemanticMemoryStoreError("source_ref 必须包含 session")
    fields: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key] = value
    session_id = fields.get("sess", "")
    if not session_id:
        raise SemanticMemoryStoreError("source_ref 必须包含 session")
    return SourceLocator(
        session_id=session_id,
        root_run_id=fields.get("root", ""),
        run_id=fields.get("run", ""),
        kind=fields.get("kind", ""),
        message_id=fields.get("msg", ""),
        tool_call_id=fields.get("tool", ""),
        step_id=fields.get("step", ""),
        episode_id=fields.get("ep", ""),
        local_id=fields.get("local", ""),
    )


@dataclass(frozen=True)
class SemanticMemoryHeader:
    id: str
    filename: str
    path: Any
    mtime: float
    name: str
    description: str | None
    type: SemanticMemoryType | None
    scope: SemanticScope
    created_at: str | None = None
    updated_at: str | None = None
    project_id: str = ""
    status: SemanticStatus = "active"
    revision: int = 0


@dataclass(frozen=True)
class SemanticMemoryRecord:
    id: str
    name: str
    description: str
    type: SemanticMemoryType
    content: str
    created_at: str
    updated_at: str
    path: Path
    scope: SemanticScope
    # Empty origin means no provenance is attached. ``explicit`` means a person
    # asked to save it and no tool evidence was attached. Source refs never
    # promote the body to a verified fact.
    origin: str = ""
    source_refs: tuple[str, ...] = ()
    schema_version: int = SEMANTIC_SCHEMA_VERSION
    project_id: str = ""
    status: SemanticStatus = "active"
    revision: int = 0
    locators: tuple[SourceLocator, ...] = ()

    def resolved_locators(self) -> tuple[SourceLocator, ...]:
        if self.locators:
            return self.locators
        return tuple(parse_locator(item) for item in self.source_refs if item)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "scope": self.scope,
            "project_id": self.project_id,
            "status": self.status,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "file": self.path.name,
            "origin": self.origin,
            "source_refs": [item.to_dict() for item in self.resolved_locators()],
        }


TYPES_SECTION = """## Memory types

Use only these four types. Do not invent another type.

<types>
<type>
  <name>user</name>
  <desc>The user's role, goals, responsibilities, and background. A useful user
  memory lets you adjust later work to their level and preferences. Collaboration
  with a senior engineer and a first-time programmer should differ. The goal is
  to know who the user is and what help is useful. Do not store negative judgments
  or facts that do not affect collaboration.</desc>
  <when>When you learn the user's role, preferences, responsibilities, or background.</when>
  <how>When that profile should change the work. When explaining code, use the mental model they already have.</how>
  <example>User: I am a data scientist investigating which logs we have → [save a user memory: the user is a data scientist currently focused on observability and logs]</example>
</type>
<type>
  <name>feedback</name>
  <desc>Guidance about how to do the work, including corrections and confirmations.
  This keeps you consistent inside a project. Record both failures and successes.
  Recording only corrections makes you overly cautious and drifts away from approaches the user already accepted.</desc>
  <when>When the user corrects you ("that's wrong", "don't do that", "stop doing X") or confirms a non-obvious approach ("yes, keep that", or quietly accepts an unusual choice). Corrections are easy to notice. Confirmations are quieter. Save the part that will still apply, especially when it is surprising or not visible in the code, and include why so the boundary can be judged later.</when>
  <how>Let the guidance change your behavior so the user does not have to say it twice.</how>
  <body>Write the rule first, then one **Why:** line (the user's reason, often an incident or a strong preference) and one **How to apply:** line (when and where it takes effect).</body>
  <example>User: don't mock the database in these tests; last quarter the mocked tests passed and the production migration failed → [save a feedback memory: integration tests must use the real database. Why: the mock hid a bad migration]</example>
</type>
<type>
  <name>project</name>
  <desc>Ongoing work, goals, plans, bugs, or incidents in this project that cannot be derived from the code or git history. Project memory explains the motive behind a request.</desc>
  <when>When you learn who is doing what, why, and by when. This changes quickly, so keep it current. Convert relative dates to absolute dates ("Thursday" → "2026-03-05") before saving.</when>
  <how>Use it to understand the request and make a better informed suggestion.</how>
  <body>Write the fact or decision first, then **Why:** and **How to apply:**. Project memory decays quickly. Why helps a later session judge whether it still holds.</body>
  <example>User: freeze all non-critical merges after Thursday; mobile is cutting a release branch → [save a project memory: merge freeze starts 2026-03-05 for the mobile release branch; remind about non-critical PRs scheduled after that]</example>
</type>
<type>
  <name>reference</name>
  <desc>A pointer to where information lives in an external system, outside the project tree.</desc>
  <when>When you learn about an external resource and what it is for, such as bugs tracked in a Linear project or feedback in a Slack channel.</when>
  <how>When the user mentions an external system, or the needed information may live there.</how>
  <example>User: pipeline bugs are tracked in the Linear project INGEST → [save a reference memory: pipeline bugs are tracked in Linear project "INGEST"]</example>
</type>
</types>
"""

WHAT_NOT_TO_SAVE = """## What not to save

- Code patterns, conventions, architecture, file paths, and project structure. Read the current project.
- Git history and who changed what. `git log` and `git blame` are the authority.
- Debugging recipes. The fix is in the code and the story is in the commit message.
- Anything already written in project docs such as CLAUDE.md or README.
- Temporary task details: work in progress, transient status, and the current conversation.

These exclusions still apply when the user explicitly asks you to save something. If they ask you to store a PR list or a status summary, ask which part is surprising or non-obvious. That is the part worth keeping."""

WHEN_TO_ACCESS = """## When to read or write memory

- When the injected in-scope index looks relevant, or the user mentions work from an earlier conversation, read it with the memory tools.
- When the user explicitly asks you to check, recall, or remember something, you must use the memory tools.
- If the user says to ignore memory or not to use it: do not apply, cite, compare, or mention any memory.
- Automatic injection includes only active global memories and active memories for the current project. Other projects and inactive records are absent.
- Do not read MEMORY.md in the memory directory to recover something that was left out. That file is a human index of the whole store and may contain other projects or stale entries.
- Memory goes stale. Treat it as true at the time it was written. Before answering or assuming from it, read the current files or resources. If it conflicts with what you observe now, trust the observation and update or deactivate the stale memory. Delete it only when the user explicitly asks you to forget it."""

TRUSTING_RECALL = """## Before recommending from memory

A memory that names a function, file, or flag only says that it existed when the memory was written. It may have been renamed, deleted, or never merged. Before recommending from it:

- If it names a file path, confirm the file exists.
- If it names a function or flag, search for it.
- If the user is about to act on the recommendation, and is not only asking about the past, verify first.

"Memory says X existed" does not mean "X exists now".

A memory that summarizes repository state (an activity log or an architecture snapshot) is frozen in time. If the user asks about the recent or current state, prefer `git log` or the code itself over that snapshot."""

FRONTMATTER_EXAMPLE = """```markdown
---
schema_version: 1
id: mem-{{stable id, independent of the title}}
name: {{editable display title}}
description: {{one sentence a later session can use to judge relevance; be specific}}
type: {{one of user, feedback, project, reference}}
scope: {{global or project}}
project_id: {{required when scope is project}}
status: {{active or inactive}}
revision: 1
---

{{body. For feedback and project, write the rule or fact, then **Why:** and **How to apply:**}}
```"""

__all__ = [
    "EXPLICIT_ORIGIN",
    "FRONTMATTER_EXAMPLE",
    "SEMANTIC_MEMORY_TYPES",
    "SEMANTIC_READ_SCOPES",
    "SEMANTIC_SCHEMA_VERSION",
    "SEMANTIC_SCOPES",
    "SEMANTIC_STATUSES",
    "TRUSTING_RECALL",
    "TYPES_SECTION",
    "WHAT_NOT_TO_SAVE",
    "WHEN_TO_ACCESS",
    "SemanticMemoryAlreadyExistsError",
    "SemanticMemoryConflictError",
    "SemanticMemoryHeader",
    "SemanticMemoryNotFoundError",
    "SemanticMemoryRecord",
    "SemanticMemoryScopeError",
    "SemanticMemoryStoreError",
    "SemanticMemoryType",
    "SemanticReadScope",
    "SemanticScope",
    "SemanticStatus",
    "SourceLocator",
    "encode_locator",
    "parse_locator",
    "parse_semantic_memory_type",
    "parse_semantic_read_scope",
    "parse_semantic_scope",
    "parse_semantic_status",
]
