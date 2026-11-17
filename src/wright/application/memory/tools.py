"""Model-facing memory tools.

They parse arguments, call MemoryService through the session's MemoryManager,
and return a ToolResult. Rules live in the service and the domain policy, not
here. The model never chooses origin; the manager supplies this turn's user
text and tool output so the service can tell them apart.
"""

from __future__ import annotations

from typing import Any

from ...domain.model.memory import FACT_KINDS, FACT_SUBJECTS
from ...domain.model.tool import ToolAccess, ToolResult
from ...domain.model.tool_definition import Tool
from .dto import WriteRequest
from .manager import MemoryManager
from .service import MemoryServiceError

_SEARCH_TYPES = ("fact", "episode", "commitment")


def _read_access(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"internal_read"}),
        subject=str(arguments.get("id") or arguments.get("query") or ""),
        reason="read long-term memory",
    )


def _write_access(arguments: dict[str, Any]) -> ToolAccess:
    return ToolAccess(
        frozenset({"persistent_write"}),
        subject=str(arguments.get("id") or arguments.get("content") or arguments.get("name") or "")[:200],
        risk_flags=("persistent_state",),
        reason="change cross-session memory",
    )


def _fail(message: str) -> ToolResult:
    return ToolResult.fail(message)


def _spaces_for(manager: MemoryManager, value: Any) -> list[str]:
    service = manager.service
    readable = list(manager.readable_space_ids)
    if value in (None, "", "current"):
        return readable
    if value == "all":
        return [space.id for space in service.list_spaces()]
    items = value if isinstance(value, list) else [value]
    resolved: list[str] = []
    for item in items:
        resolved.append(service.resolve_space(str(item), current=manager.repo_space_id or manager.current_space_id))
    return resolved


def build_memory_tools(manager: MemoryManager) -> list[Tool]:
    def write(args: dict[str, Any], runtime: Any = None) -> ToolResult:
        del runtime
        try:
            space_id = manager.service.resolve_space(
                str(args.get("space") or ""), current=manager.current_space_id, allow_create=True,
            )
        except (MemoryServiceError, ValueError) as exc:
            return _fail(str(exc))
        user_texts, tool_texts = manager.turn_sources()
        markers = args.get("markers")
        outcome = manager.service.write_fact(WriteRequest(
            content=str(args.get("content") or ""),
            kind=str(args.get("kind") or ""),
            space_id=space_id,
            subject=str(args.get("subject") or "other"),
            why=str(args.get("why") or ""),
            markers=tuple(str(item) for item in markers) if isinstance(markers, list) else (),
            pin=bool(args.get("pin", False)),
            sensitivity_hint="sensitive" if args.get("sensitive") else "",
            supersedes=str(args.get("supersedes") or ""),
            actor="agent",
            user_texts=user_texts,
            external_texts=tool_texts,
        ))
        if outcome.status in {"rejected", "needs_user"}:
            return ToolResult.fail(outcome.message, outcome.to_dict())
        return ToolResult.success(outcome.to_dict())

    def update(args: dict[str, Any], runtime: Any = None) -> ToolResult:
        del runtime
        fact_id = str(args.get("id") or "")
        user_texts, tool_texts = manager.turn_sources()
        pin = args.get("pin")
        tier = None if pin is None else ("pinned" if pin else "indexed")
        markers = args.get("markers")
        try:
            fact = manager.service.update_fact(
                fact_id,
                actor="agent",
                expected_revision=args.get("expected_revision"),
                content=args.get("content"),
                why=args.get("why"),
                kind=args.get("kind"),
                tier=tier,
                markers=[str(item) for item in markers] if isinstance(markers, list) else None,
                user_texts=user_texts,
                external_texts=tool_texts,
            )
        except (MemoryServiceError, ValueError) as exc:
            return _fail(str(exc))
        except Exception as exc:
            return _fail(f"update failed: {exc}")
        return ToolResult.success({"fact": fact.to_dict()})

    def forget(args: dict[str, Any], runtime: Any = None) -> ToolResult:
        del runtime
        try:
            removed = manager.service.forget(str(args.get("id") or ""), actor="agent", reason=str(args.get("reason") or ""))
        except MemoryServiceError as exc:
            return _fail(str(exc))
        return ToolResult.success({"removed": removed})

    def search(args: dict[str, Any], runtime: Any = None) -> ToolResult:
        del runtime
        try:
            spaces = _spaces_for(manager, args.get("spaces"))
        except (MemoryServiceError, ValueError) as exc:
            return _fail(str(exc))
        types = args.get("types")
        chosen = tuple(item for item in (types if isinstance(types, list) else _SEARCH_TYPES) if item in _SEARCH_TYPES)
        limit = max(1, min(int(args.get("limit") or 10), 30))
        hits = manager.service.search(
            str(args.get("query") or ""),
            space_ids=spaces,
            types=chosen or _SEARCH_TYPES,
            limit=limit,
            include_inactive=bool(args.get("include_inactive", False)),
        )
        return ToolResult.success({"spaces": spaces, "results": [hit.to_dict() for hit in hits]})

    def read(args: dict[str, Any], runtime: Any = None) -> ToolResult:
        del runtime
        item_id = str(args.get("id") or "")
        service = manager.service
        if item_id.startswith("f-"):
            fact = service.get_fact(item_id)
            if fact is None:
                return _fail(f"no such fact: {item_id}")
            service.touch([item_id])
            payload: dict[str, Any] = {"fact": fact.to_dict()}
            if args.get("include_changes"):
                payload["changes"] = [change.to_dict() for change in service.changes(target_id=item_id, limit=20)]
            return ToolResult.success(payload)
        if item_id.startswith("ep-"):
            episode = service.get_episode(item_id)
            if episode is None:
                return _fail(f"no such episode: {item_id}")
            payload = {"episode": episode.to_dict()}
            if args.get("include_evidence"):
                payload["evidence"] = [read.to_dict() for read in service.read_evidence(episode)]
            return ToolResult.success(payload)
        if item_id.startswith("c-"):
            items = [item for item in service.list_commitments() if item.id == item_id]
            if not items:
                return _fail(f"no such commitment: {item_id}")
            return ToolResult.success({"commitment": items[0].to_dict()})
        return _fail("id must start with f-, ep- or c-")

    def space(args: dict[str, Any], runtime: Any = None) -> ToolResult:
        del runtime
        action = str(args.get("action") or "list")
        if action == "enter":
            name = str(args.get("name") or "").strip()
            if not name:
                return _fail("name is required")
            try:
                entered = manager.enter_space(name)
            except (MemoryServiceError, ValueError) as exc:
                return _fail(str(exc))
            return ToolResult.success({"entered": entered, "readable": list(manager.readable_space_ids)})
        if action == "leave":
            return ToolResult.success({"left": manager.leave_space(), "readable": list(manager.readable_space_ids)})
        return ToolResult.success({
            "current": manager.current_space_id,
            "readable": list(manager.readable_space_ids),
            "spaces": [item.to_dict() for item in manager.service.list_spaces()],
        })

    def commitment_create(args: dict[str, Any], runtime: Any = None) -> ToolResult:
        del runtime
        try:
            space_id = manager.service.resolve_space(
                str(args.get("space") or ""), current=manager.current_space_id, allow_create=True,
            )
            item = manager.service.create_commitment(
                str(args.get("content") or ""), space_id=space_id, due=str(args.get("due") or ""),
            )
        except (MemoryServiceError, ValueError) as exc:
            return _fail(str(exc))
        return ToolResult.success({"commitment": item.to_dict()})

    def commitment_update(args: dict[str, Any], runtime: Any = None) -> ToolResult:
        del runtime
        try:
            item = manager.service.update_commitment(
                str(args.get("id") or ""),
                status=args.get("status"),
                due=args.get("due"),
                content=args.get("content"),
            )
        except (MemoryServiceError, ValueError) as exc:
            return _fail(str(exc))
        return ToolResult.success({"commitment": item.to_dict()})

    space_help = (
        'Where it applies: "current" (default: the entered personal space, else this project), '
        '"global" (about the user, everywhere), "repo" (this project), or a personal space name.'
    )
    return [
        Tool(
            name="memory_write",
            description=(
                "Save one self-contained fact to long-term memory. Origin and sensitivity are decided by the "
                "system: text copied from tool output is quarantined until the user confirms it, and sensitive "
                "personal details are refused unless the user asked you to remember them. Credentials are refused."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The fact, readable on its own."},
                    "kind": {"type": "string", "enum": list(FACT_KINDS)},
                    "subject": {"type": "string", "enum": list(FACT_SUBJECTS), "default": "other"},
                    "why": {"type": "string", "description": "Why it matters or when it applies."},
                    "space": {"type": "string", "description": space_help},
                    "markers": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Distinctive names, hostnames or code names in the fact. They keep it from leaking.",
                    },
                    "pin": {"type": "boolean", "default": False, "description": "Show in full in every conversation."},
                    "supersedes": {"type": "string", "description": "Id of an older fact this one replaces."},
                    "sensitive": {"type": "boolean", "default": False, "description": "Mark as sensitive even if the rules would not."},
                },
                "required": ["content", "kind"],
                "additionalProperties": False,
            },
            call=write,
            access_descriptor=_write_access,

        ),
        Tool(
            name="memory_update",
            description="Change an existing fact. Use memory_forget to remove one, and memory_write with supersedes when the old fact became false.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "why": {"type": "string"},
                    "kind": {"type": "string", "enum": list(FACT_KINDS)},
                    "markers": {"type": "array", "items": {"type": "string"}},
                    "pin": {"type": "boolean", "description": "true pins it, false moves it back to the index."},
                    "expected_revision": {"type": "integer", "description": "Revision you read; stale writes are refused."},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            call=update,
            access_descriptor=_write_access,
            defer_to_model=True,
        ),
        Tool(
            name="memory_forget",
            description="Permanently remove a fact (f-), episode (ep-) or commitment (c-), and facts inferred only from it.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            call=forget,
            access_descriptor=_write_access,
            defer_to_model=True,
        ),
        Tool(
            name="memory_search",
            description="Search facts, past episodes and commitments. Defaults to the readable spaces of this session.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words to look for; empty lists the newest."},
                    "spaces": {
                        "description": '"current" (default), "all", or a list of spaces such as ["global", "repo", "装修"].',
                        "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    },
                    "types": {"type": "array", "items": {"type": "string", "enum": list(_SEARCH_TYPES)}},
                    "limit": {"type": "integer", "default": 10},
                    "include_inactive": {"type": "boolean", "default": False},
                },
                "required": [],
                "additionalProperties": False,
            },
            call=search,
            access_descriptor=_read_access,
            is_concurrency_safe=lambda args: True,
        ),
        Tool(
            name="memory_read",
            description="Read one fact, episode or commitment in full.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "include_evidence": {"type": "boolean", "default": False, "description": "Episodes: load the recorded sources."},
                    "include_changes": {"type": "boolean", "default": False, "description": "Facts: show the change history."},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            call=read,
            access_descriptor=_read_access,
            is_concurrency_safe=lambda args: True,
        ),
        Tool(
            name="memory_space",
            description="List memory spaces, or enter / leave a personal space (a topic the user keeps apart).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "enter", "leave"], "default": "list"},
                    "name": {"type": "string", "description": "Personal space name for enter."},
                },
                "required": [],
                "additionalProperties": False,
            },
            call=space,
            access_descriptor=_read_access,
            defer_to_model=True,
        ),
        Tool(
            name="commitment_create",
            description="Record something you promised to do or to remind the user about. It shows up again when due.",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "due": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM, local time."},
                    "space": {"type": "string", "description": space_help},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            call=commitment_create,
            access_descriptor=_write_access,
            defer_to_model=True,
        ),
        Tool(
            name="commitment_update",
            description="Mark a commitment done or dismissed, or change its text or due date.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "done", "dismissed"]},
                    "due": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            call=commitment_update,
            access_descriptor=_write_access,
            defer_to_model=True,
        ),
    ]


MEMORY_READ_TOOLS = frozenset({"memory_search", "memory_read", "memory_space"})
MEMORY_WRITE_TOOLS = frozenset({"memory_write", "memory_update", "memory_forget", "commitment_create", "commitment_update"})

__all__ = ["MEMORY_READ_TOOLS", "MEMORY_WRITE_TOOLS", "build_memory_tools"]
