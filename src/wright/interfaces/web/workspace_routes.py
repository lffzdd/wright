"""Workspace, review, memory, and schedule routes.

Handlers validate HTTP input and call application services. They do not
decide permission, memory, or schedule rules themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ...application.session.dispatch import SLASH_COMMANDS
from ...application.workspace.catalog import WorkspaceCatalogError
from ...application.workspace.documents import DocumentError
from ...application.workspace.files import PathRejected, list_directory, read_text
from ...application.workspace.git_status import summarize_git
from ...application.workspace.grants import (
    GrantError,
    add_session_rule,
    change_directory,
    list_grants,
    revoke_persistent_rule,
    revoke_session_rule,
)
from ...application.workspace.journal import ChangeJournalError, SessionChangeJournal
from ...application.workspace.memory_view import (
    MemoryViewError,
    bound_project,
    create_semantic,
    delete_episode,
    delete_semantic,
    project_memory,
    update_core,
    update_semantic,
)
from ...application.workspace.rules import (
    RuleError,
    delete_rule,
    get_rule,
    list_rules,
    save_rule,
)
from ...application.workspace.schedule_api import (
    ScheduleApiError,
    create_schedule,
    delete_schedule,
    list_project_schedules,
    list_runs,
    pause_schedule,
    project_action,
    resume_schedule,
    update_schedule,
)
from ...application.workspace.search import (
    search_content,
    search_files,
    search_symbols,
    search_tasks,
)
from ...core.paths import project_id
from .auth import BootstrapAuth
from .runtime_manager import RuntimeManager, RuntimeManagerError


class WorkspaceCreate(BaseModel):
    path: str


class WorkspaceSelect(BaseModel):
    project_id: str


class WorkspaceSessionBody(BaseModel):
    environment: str | None = None
    model: str | None = None
    prompt: str | None = None
    resume_session_id: str | None = None


class PolicyBody(BaseModel):
    interaction_mode: str | None = None
    permission_mode: str | None = None


class ConfirmBody(BaseModel):
    confirm: bool = False
    paths: list[str] = Field(default_factory=list)


class DirectoryBody(BaseModel):
    path: str
    scope: str = "session"
    action: str
    confirm: bool = False


class RuleBody(BaseModel):
    confirm: bool = False
    rule: dict[str, Any] | None = None


class CoreBody(BaseModel):
    section: str
    content: str
    mode: str = "append"


class SemanticBody(BaseModel):
    name: str
    content: str
    description: str = ""
    type: str = "project"
    scope: str = "project"
    confirm: bool = False
    expected_revision: int | None = None


class SkillBody(BaseModel):
    scope: str = "project"
    description: str = ""
    body: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    confirm: bool = False


class ScheduleBody(BaseModel):
    name: str | None = None
    prompt: str | None = None
    trigger: dict[str, Any] | None = None
    recovery_policy: str | None = None
    max_retries: int | None = None
    retry_delay_seconds: float | None = None
    confirm: bool = False


def add_workspace_routes(router: APIRouter, manager: RuntimeManager, auth: BootstrapAuth) -> None:
    def require(request: Request) -> None:
        from .auth import COOKIE_NAME

        if not auth.valid(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(status_code=401, detail="authentication required")

    @router.get("/commands")
    def commands(request: Request) -> list[dict[str, str]]:
        require(request)
        return [
            {"name": command.name, "description": command.description, "usage": command.usage}
            for command in SLASH_COMMANDS.values()
            if command.handler is not None
        ]

    @router.get("/workspaces")
    def workspaces(request: Request) -> dict[str, Any]:
        require(request)
        return manager.workspaces()

    @router.post("/workspaces")
    def register_workspace(body: WorkspaceCreate, request: Request) -> dict[str, Any]:
        require(request)
        try:
            return manager.register_workspace(body.path)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 400, detail=str(exc)) from exc

    @router.post("/workspaces/select")
    def select_workspace(body: WorkspaceSelect, request: Request) -> dict[str, Any]:
        require(request)
        try:
            return manager.select_workspace(body.project_id)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @router.delete("/workspaces/{registered_id}")
    def unregister_workspace(registered_id: str, request: Request) -> dict[str, Any]:
        require(request)
        try:
            return manager.unregister_workspace(registered_id)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @router.get("/workspaces/{registered_id}/sessions")
    def workspace_sessions(registered_id: str, request: Request) -> list[dict[str, Any]]:
        require(request)
        try:
            return manager.workspace_sessions(registered_id)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @router.post("/workspaces/{registered_id}/sessions")
    async def workspace_create_session(registered_id: str, request: Request) -> dict[str, Any]:
        require(request)
        payload: dict[str, Any] = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            raw = await request.json()
            if isinstance(raw, dict):
                payload = raw
        body = WorkspaceSessionBody.model_validate(payload)
        try:
            return manager.create_in_workspace(
                registered_id,
                environment=body.environment,
                model=body.model,
                prompt=body.prompt,
                resume_session_id=body.resume_session_id,
            ).snapshot()
        except (RuntimeManagerError, WorkspaceCatalogError) as exc:
            status = getattr(exc, "status_code", None) or 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/workspaces/{registered_id}/tree")
    def workspace_tree(registered_id: str, request: Request, path: str = "") -> dict[str, Any]:
        require(request)
        root = _project_root(manager, registered_id)
        return _files(root, path, listing=True)

    @router.get("/workspaces/{registered_id}/file")
    def workspace_file(registered_id: str, request: Request, path: str) -> dict[str, Any]:
        require(request)
        return _files(_project_root(manager, registered_id), path, listing=False)

    @router.get("/workspaces/{registered_id}/search")
    def workspace_search(
        registered_id: str, request: Request, q: str = "", kind: str = "file",
    ) -> dict[str, Any]:
        require(request)
        root = _project_root(manager, registered_id)
        sessions = manager.workspace_sessions(registered_id)
        return {"kind": kind, "results": _search(root, sessions, q, kind)}

    @router.get("/workspaces/{registered_id}/schedules")
    def project_schedules(registered_id: str, request: Request) -> list[dict[str, Any]]:
        require(request)
        return list_project_schedules(_project_root(manager, registered_id))

    @router.post("/workspaces/{registered_id}/schedules/{schedule_id}/{action}")
    def project_schedule_action(
        registered_id: str,
        schedule_id: str,
        action: str,
        body: ScheduleBody,
        request: Request,
    ) -> dict[str, Any]:
        require(request)
        if action not in {"pause", "resume", "update", "delete"}:
            raise HTTPException(status_code=400, detail="unsupported schedule action")
        try:
            return project_action(
                _project_root(manager, registered_id),
                schedule_id,
                action,
                name=body.name,
                prompt=body.prompt,
                trigger=body.trigger,
                recovery_policy=body.recovery_policy,
                max_retries=body.max_retries,
                retry_delay_seconds=body.retry_delay_seconds,
                confirm=body.confirm,
            )
        except ScheduleApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/workspace")
    def session_workspace(session_id: str, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        root = Path(handle.runtime.session_state.workspace_dir)
        project = Path(handle.runtime.session_state.project_root or root)
        return {
            "session_id": session_id,
            "project_id": project_id(project),
            "project_root": str(project),
            "execution_root": str(root),
            "environment": handle.runtime.session_state.environment,
            "base_commit": handle.runtime.session_state.base_commit,
            "branch_name": handle.runtime.session_state.branch_name,
            **summarize_git(root),
        }

    @router.get("/sessions/{session_id}/tree")
    def session_tree(session_id: str, request: Request, path: str = "") -> dict[str, Any]:
        require(request)
        return _files(_execution_root(manager, session_id), path, listing=True)

    @router.get("/sessions/{session_id}/file")
    def session_file(session_id: str, request: Request, path: str) -> dict[str, Any]:
        require(request)
        return _files(_execution_root(manager, session_id), path, listing=False)

    @router.get("/sessions/{session_id}/search")
    def session_search(session_id: str, request: Request, q: str = "", kind: str = "file") -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        root = _execution_root(manager, session_id)
        owner = getattr(handle, "owner", manager.directory)
        return {"kind": kind, "results": _search(root, owner.list_sessions(), q, kind)}

    @router.post("/sessions/{session_id}/policy")
    def set_policy(session_id: str, body: PolicyBody, request: Request) -> dict[str, Any]:
        require(request)
        try:
            return _handle(manager, session_id).set_execution_policy(
                interaction_mode=body.interaction_mode,
                permission_mode=body.permission_mode,
            )
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 409, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/grants")
    def grants(session_id: str, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        return list_grants(handle.runtime.session_state, handle.runtime.permission_settings)

    @router.post("/sessions/{session_id}/grants")
    def create_grant(session_id: str, body: RuleBody, request: Request) -> dict[str, Any]:
        require(request)
        if not body.confirm or not isinstance(body.rule, dict):
            raise HTTPException(status_code=400, detail="confirmation and a rule are required")
        handle = _handle(manager, session_id)
        _require_idle(handle)
        try:
            created = add_session_rule(handle.runtime.session_state, body.rule)
        except (GrantError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        handle.service.persist()
        return created

    @router.post("/sessions/{session_id}/grants/{rule_id}/revoke")
    def revoke_grant(session_id: str, rule_id: str, body: ConfirmBody, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        _require_idle(handle)
        try:
            try:
                result = revoke_session_rule(
                    handle.runtime.session_state, rule_id, confirm=body.confirm,
                )
            except GrantError:
                result = revoke_persistent_rule(
                    handle.runtime.permission_settings, rule_id, confirm=body.confirm,
                )
        except GrantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result.get("scope") == "session":
            handle.service.persist()
        return result

    @router.post("/sessions/{session_id}/directories")
    def change_directory_grant(
        session_id: str, body: DirectoryBody, request: Request,
    ) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        _require_idle(handle)
        try:
            result = change_directory(
                handle.runtime.session_state,
                handle.runtime.permission_settings,
                body.path,
                scope=body.scope,
                confirm=body.confirm,
                action=body.action,
            )
        except GrantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result["scope"] == "session":
            handle.service.persist()
        return result

    @router.get("/sessions/{session_id}/review")
    def review(session_id: str, request: Request) -> dict[str, Any]:
        require(request)
        return _journal(manager, session_id).projection()

    @router.post("/sessions/{session_id}/review/accept")
    def accept_review(session_id: str, body: ConfirmBody, request: Request) -> dict[str, Any]:
        require(request)
        return _review_action(manager, session_id, body, "accept")

    @router.post("/sessions/{session_id}/review/revert")
    def revert_review(session_id: str, body: ConfirmBody, request: Request) -> dict[str, Any]:
        require(request)
        return _review_action(manager, session_id, body, "revert")

    @router.get("/sessions/{session_id}/memory")
    def memory(session_id: str, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        try:
            return project_memory(_memory(handle), bound_project_id=bound_project(handle.runtime.session_state))
        except MemoryViewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.put("/sessions/{session_id}/memory/core")
    def put_core(session_id: str, body: CoreBody, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        try:
            return update_core(
                _memory(handle),
                bound_project_id=bound_project(handle.runtime.session_state),
                section=body.section,
                content=body.content,
                mode=body.mode,
            )
        except MemoryViewError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/memory/semantic")
    def post_semantic(session_id: str, body: SemanticBody, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        try:
            return create_semantic(
                _memory(handle),
                bound_project_id=bound_project(handle.runtime.session_state),
                name=body.name,
                content=body.content,
                description=body.description,
                type_=body.type,
                scope=body.scope,
            )
        except MemoryViewError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/sessions/{session_id}/memory/semantic/{memory_id}")
    def patch_semantic(session_id: str, memory_id: str, body: SemanticBody, request: Request) -> dict[str, Any]:
        require(request)
        if body.expected_revision is None:
            raise HTTPException(status_code=400, detail="expected_revision is required")
        handle = _handle(manager, session_id)
        fields = {
            "name": body.name,
            "content": body.content,
            "description": body.description,
            "type_": body.type,
        }
        try:
            return update_semantic(
                _memory(handle),
                memory_id,
                bound_project_id=bound_project(handle.runtime.session_state),
                expected_revision=body.expected_revision,
                fields=fields,
            )
        except MemoryViewError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/sessions/{session_id}/memory/semantic/{memory_id}")
    def remove_semantic(session_id: str, memory_id: str, body: ConfirmBody, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        try:
            return delete_semantic(
                _memory(handle),
                memory_id,
                bound_project_id=bound_project(handle.runtime.session_state),
                confirm=body.confirm,
            )
        except MemoryViewError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/sessions/{session_id}/memory/episodes/{episode_id}")
    def remove_episode(session_id: str, episode_id: str, body: ConfirmBody, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        try:
            return delete_episode(_memory(handle), episode_id, confirm=body.confirm)
        except MemoryViewError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/rules")
    def rules(session_id: str, request: Request) -> dict[str, Any]:
        require(request)
        return list_rules(_project_of(manager, session_id))

    @router.get("/sessions/{session_id}/rules/{skill_id}")
    def rule(session_id: str, skill_id: str, request: Request) -> dict[str, Any]:
        require(request)
        try:
            return get_rule(_project_of(manager, session_id), skill_id)
        except RuleError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/sessions/{session_id}/rules/{skill_id}")
    def put_rule(session_id: str, skill_id: str, body: SkillBody, request: Request) -> dict[str, Any]:
        require(request)
        if body.scope not in {"project", "user"}:
            raise HTTPException(status_code=400, detail="scope must be project or user")
        try:
            return save_rule(
                _project_of(manager, session_id),
                skill_id,
                scope=body.scope,  # type: ignore[arg-type]
                description=body.description,
                body=body.body,
                allowed_tools=body.allowed_tools,
                confirm=body.confirm,
            )
        except RuleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/sessions/{session_id}/rules/{skill_id}")
    def remove_rule(
        session_id: str, skill_id: str, body: SkillBody, request: Request,
    ) -> dict[str, Any]:
        require(request)
        if body.scope not in {"project", "user"}:
            raise HTTPException(status_code=400, detail="scope must be project or user")
        try:
            return delete_rule(
                _project_of(manager, session_id),
                skill_id,
                scope=body.scope,  # type: ignore[arg-type]
                confirm=body.confirm,
            )
        except RuleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/schedules")
    def session_schedules(session_id: str, request: Request) -> list[dict[str, Any]]:
        require(request)
        handle = _handle(manager, session_id)
        project = Path(handle.runtime.session_state.project_root or handle.runtime.session_state.workspace_dir)
        rows = list_project_schedules(project)
        return [row for row in rows if row.get("session_id") == handle.runtime.session_state.session_id]

    @router.post("/sessions/{session_id}/schedules")
    def post_schedule(session_id: str, body: ScheduleBody, request: Request) -> dict[str, Any]:
        require(request)
        if not body.name or not body.prompt or not body.trigger:
            raise HTTPException(status_code=400, detail="name, prompt, and trigger are required")
        handle = _handle(manager, session_id)
        try:
            return create_schedule(
                handle.runtime,
                name=body.name,
                prompt=body.prompt,
                trigger=body.trigger,
                recovery_policy=body.recovery_policy or "manual",
                max_retries=body.max_retries or 0,
                retry_delay_seconds=body.retry_delay_seconds or 30,
            )
        except (ScheduleApiError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/sessions/{session_id}/schedules/{schedule_id}")
    def patch_schedule(session_id: str, schedule_id: str, body: ScheduleBody, request: Request) -> dict[str, Any]:
        require(request)
        handle = _handle(manager, session_id)
        try:
            return update_schedule(
                handle.runtime,
                schedule_id,
                name=body.name,
                prompt=body.prompt,
                trigger=body.trigger,
                recovery_policy=body.recovery_policy,
                max_retries=body.max_retries,
                retry_delay_seconds=body.retry_delay_seconds,
            )
        except ScheduleApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/schedules/{schedule_id}/pause")
    def pause(session_id: str, schedule_id: str, request: Request) -> dict[str, Any]:
        require(request)
        return _schedule_call(manager, session_id, lambda runtime: pause_schedule(runtime, schedule_id))

    @router.post("/sessions/{session_id}/schedules/{schedule_id}/resume")
    def resume(session_id: str, schedule_id: str, request: Request) -> dict[str, Any]:
        require(request)
        return _schedule_call(manager, session_id, lambda runtime: resume_schedule(runtime, schedule_id))

    @router.delete("/sessions/{session_id}/schedules/{schedule_id}")
    def remove_schedule(session_id: str, schedule_id: str, body: ConfirmBody, request: Request) -> dict[str, Any]:
        require(request)
        return _schedule_call(
            manager, session_id, lambda runtime: delete_schedule(runtime, schedule_id, confirm=body.confirm),
        )

    @router.get("/sessions/{session_id}/schedules/{schedule_id}/runs")
    def schedule_runs(session_id: str, schedule_id: str, request: Request) -> list[dict[str, Any]]:
        require(request)
        handle = _handle(manager, session_id)
        project = Path(handle.runtime.session_state.project_root or handle.runtime.session_state.workspace_dir)
        try:
            return list_runs(project, handle.runtime.session_state.session_id, schedule_id)
        except ScheduleApiError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/documents")
    async def upload_document(
        session_id: str,
        request: Request,
        file: Annotated[UploadFile, File(...)],
    ) -> dict[str, Any]:
        require(request)
        try:
            return _handle(manager, session_id).upload_document(file.filename or "file", await file.read())
        except (RuntimeManagerError, DocumentError) as exc:
            status = getattr(exc, "status_code", None) or 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        finally:
            await file.close()


def _handle(manager: RuntimeManager, session_id: str):
    try:
        return manager.get(session_id)
    except RuntimeManagerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _execution_root(manager: RuntimeManager, session_id: str) -> Path:
    return Path(_handle(manager, session_id).runtime.session_state.workspace_dir)


def _project_of(manager: RuntimeManager, session_id: str) -> Path:
    state = _handle(manager, session_id).runtime.session_state
    return Path(state.project_root or state.workspace_dir)


def _project_root(manager: RuntimeManager, registered_id: str) -> Path:
    try:
        record = manager._catalog().get(registered_id)
    except WorkspaceCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Path(str(record["root"]))


def _files(root: Path, path: str, *, listing: bool) -> dict[str, Any]:
    try:
        if listing:
            return list_directory(root, path)
        return read_text(root, path)
    except PathRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _search(root: Path, sessions: list[dict[str, Any]], query: str, kind: str) -> list[dict[str, Any]]:
    if kind == "file":
        return search_files(root, query)
    if kind == "content":
        return search_content(root, query)
    if kind == "symbol":
        return search_symbols(root, query)
    if kind == "task":
        return search_tasks(sessions, query)
    raise HTTPException(status_code=400, detail="kind must be file, content, symbol, or task")


def _journal(manager: RuntimeManager, session_id: str) -> SessionChangeJournal:
    return SessionChangeJournal(_handle(manager, session_id).runtime.session_state)


def _review_action(manager: RuntimeManager, session_id: str, body: ConfirmBody, action: str) -> dict[str, Any]:
    journal = _journal(manager, session_id)
    try:
        if action == "accept":
            return journal.accept(body.paths, confirm=body.confirm)
        return journal.revert(body.paths, confirm=body.confirm)
    except ChangeJournalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _memory(handle: Any):
    agent = getattr(handle.runtime, "agent", None)
    memory = getattr(agent, "memory", None)
    return getattr(memory, "service", None)


def _require_idle(handle: Any) -> None:
    if not handle.runtime.agent_idle.is_set():
        raise HTTPException(
            status_code=409,
            detail="a turn is still executing; grants change on the next resolution once it is idle",
        )


def _schedule_call(manager: RuntimeManager, session_id: str, call) -> dict[str, Any]:
    handle = _handle(manager, session_id)
    try:
        return call(handle.runtime)
    except ScheduleApiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
