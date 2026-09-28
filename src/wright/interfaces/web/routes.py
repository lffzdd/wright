"""REST API routes for Wright Web interface."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ...infrastructure.workspace.worktrees import WorktreeError
from ..i18n import t
from ..i18n.locale import LOCALE_LABELS, SUPPORTED_LOCALES, get_locale, set_locale
from .auth import COOKIE_NAME, BootstrapAuth
from .diff import DiffError, change_patch, list_changes
from .runtime_manager import RuntimeManager, RuntimeManagerError


class BootstrapRequest(BaseModel):
    token: str


class SessionRequest(BaseModel):
    environment: str | None = None
    model: str | None = None
    prompt: str | None = None
    resume_session_id: str | None = None


class ModelRequest(BaseModel):
    model: str


class PreferenceRequest(BaseModel):
    interface_language: str


def _require_auth(request: Request, auth: BootstrapAuth) -> None:
    if not auth.valid(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="authentication required")


def create_api_router(manager: RuntimeManager, auth: BootstrapAuth) -> APIRouter:
    """Create and return the REST API router for Wright Web services."""
    router = APIRouter(prefix="/api/v1")

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "wright-web", "version": 1}

    @router.post("/auth/exchange")
    def exchange(body: BootstrapRequest, response: Response) -> dict[str, bool]:
        session_token = auth.exchange(body.token)
        if session_token is None:
            raise HTTPException(status_code=401, detail="invalid or already-used bootstrap token")
        response.set_cookie(
            COOKIE_NAME,
            session_token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return {"ok": True}

    @router.get("/preferences")
    def preferences(request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        locale = get_locale()
        return {
            "interface_language": locale,
            "supported": [
                {"id": item, "label": LOCALE_LABELS[item]} for item in SUPPORTED_LOCALES
            ],
            "note": t("web.language_note"),
        }

    @router.put("/preferences")
    def update_preferences(body: PreferenceRequest, request: Request) -> dict[str, str]:
        _require_auth(request, auth)
        locale = set_locale(body.interface_language, persist=True)
        return {"interface_language": locale}

    @router.get("/project")
    def project(request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        return manager.project()

    @router.get("/sessions")
    def sessions(request: Request) -> list[dict[str, Any]]:
        _require_auth(request, auth)
        return manager.list_sessions()

    @router.post("/sessions")
    def create_session(body: SessionRequest, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return manager.create(
                environment=body.environment,
                model=body.model,
                prompt=body.prompt,
                resume_session_id=body.resume_session_id,
            ).snapshot()
        except (RuntimeManagerError, ValueError, WorktreeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/snapshot")
    def snapshot(session_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return manager.get(session_id).snapshot()
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/commands/{command_id}")
    def command_status(session_id: str, command_id: str, request: Request) -> dict[str, Any]:
        """Query a durably accepted command after a client disconnect."""
        _require_auth(request, auth)
        try:
            return manager.get(session_id).command_status(command_id)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}")
    def durable_run_history(run_id: str, request: Request) -> dict[str, Any]:
        """Read persisted automation history after its source Session closes."""
        _require_auth(request, auth)
        try:
            return manager.run_history(run_id)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/model")
    def set_session_model(session_id: str, body: ModelRequest, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return manager.set_model(session_id, body.model)
        except RuntimeManagerError as exc:
            raise HTTPException(
                status_code=exc.status_code or 409,
                detail=str(exc),
            ) from exc

    @router.post("/sessions/{session_id}/attachments")
    async def upload_attachment(
        session_id: str,
        request: Request,
        file: Annotated[UploadFile, File(...)],
    ) -> dict[str, object]:
        _require_auth(request, auth)
        try:
            return manager.get(session_id).upload_attachment(
                file.filename or "image", await file.read()
            )
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 409, detail=str(exc)) from exc
        finally:
            await file.close()

    @router.get("/sessions/{session_id}/attachments/{attachment_id}")
    def get_attachment(session_id: str, attachment_id: str, request: Request) -> FileResponse:
        _require_auth(request, auth)
        try:
            record, path = manager.get(session_id).attachment_path(attachment_id)
            return FileResponse(
                path,
                media_type=record.media_type,
                filename=record.filename,
                content_disposition_type="inline",
            )
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/attachments/{attachment_id}/thumbnail")
    def get_attachment_thumbnail(session_id: str, attachment_id: str, request: Request) -> FileResponse:
        _require_auth(request, auth)
        try:
            _record, path = manager.get(session_id).attachment_thumbnail_path(attachment_id)
            return FileResponse(path, media_type="image/webp")
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/artifacts/{artifact_id}")
    def get_artifact(session_id: str, artifact_id: str, request: Request) -> FileResponse:
        """Authenticated delivery of a registered tool artifact only."""
        _require_auth(request, auth)
        try:
            ref, path = manager.get(session_id).artifact_path(artifact_id)
            return FileResponse(
                path, media_type=ref.media_type, filename=ref.name,
                content_disposition_type="inline",
            )
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @router.delete("/sessions/{session_id}/attachments/{attachment_id}")
    def delete_attachment(session_id: str, attachment_id: str, request: Request) -> dict[str, bool]:
        _require_auth(request, auth)
        try:
            manager.get(session_id).remove_attachment(attachment_id)
            return {"ok": True}
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 409, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/close")
    def close_session(session_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return manager.close(session_id)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/archive")
    def archive_session(session_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            result = manager.archive(session_id)
            return {
                "removed": result.removed,
                "retained": result.retained,
                "reason": result.reason,
                "path": str(result.path),
                "branch_name": result.branch_name,
            }
        except (RuntimeManagerError, ValueError, WorktreeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/changes")
    def changes(session_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return list_changes(manager.get(session_id).runtime.project_context)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DiffError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/changes/{path:path}")
    def change(session_id: str, path: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return change_patch(manager.get(session_id).runtime.project_context, path)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DiffError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
