"""FastAPI surface and launcher for Wright's local Web console."""

from __future__ import annotations

import asyncio
import json
import queue
import socket
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..worktrees import WorktreeError
from .auth import BootstrapAuth
from .diff import DiffError, change_patch, list_changes
from .runtime_manager import RuntimeManager, RuntimeManagerError

COOKIE_NAME = "wright_web_session"


class BootstrapRequest(BaseModel):
    token: str


class SessionRequest(BaseModel):
    environment: str | None = None
    model: str | None = None
    prompt: str | None = None
    resume_session_id: str | None = None


class ModelRequest(BaseModel):
    model: str


def _origin_for(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', '')}"


def _require_auth(request: Request, auth: BootstrapAuth) -> None:
    if not auth.valid(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="authentication required")


def create_app(
    manager: RuntimeManager,
    auth: BootstrapAuth,
    *,
    static_dir: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(title="Wright Local Web", docs_url=None, redoc_url=None, lifespan=lifespan)
    assets = (static_dir or Path(__file__).parent / "static").resolve()

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return Response("invalid host", status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin != _origin_for(request):
                return Response("invalid origin", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "wright-web", "version": 1}

    @app.post("/api/v1/auth/exchange")
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

    @app.get("/api/v1/project")
    def project(request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        return manager.project()

    @app.get("/api/v1/sessions")
    def sessions(request: Request) -> list[dict[str, Any]]:
        _require_auth(request, auth)
        return manager.list_sessions()

    @app.post("/api/v1/sessions")
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

    @app.get("/api/v1/sessions/{session_id}/snapshot")
    def snapshot(session_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return manager.get(session_id).snapshot()
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/sessions/{session_id}/commands/{command_id}")
    def command_status(session_id: str, command_id: str, request: Request) -> dict[str, Any]:
        """Query a durably accepted command after a client disconnect."""
        _require_auth(request, auth)
        try:
            return manager.get(session_id).command_status(command_id)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/model")
    def set_session_model(session_id: str, body: ModelRequest, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return manager.set_model(session_id, body.model)
        except RuntimeManagerError as exc:
            raise HTTPException(
                status_code=exc.status_code or 409,
                detail=str(exc),
            ) from exc

    @app.post("/api/v1/sessions/{session_id}/attachments")
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

    @app.get("/api/v1/sessions/{session_id}/attachments/{attachment_id}")
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

    @app.get("/api/v1/sessions/{session_id}/attachments/{attachment_id}/thumbnail")
    def get_attachment_thumbnail(session_id: str, attachment_id: str, request: Request) -> FileResponse:
        _require_auth(request, auth)
        try:
            _record, path = manager.get(session_id).attachment_thumbnail_path(attachment_id)
            return FileResponse(path, media_type="image/webp")
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 404, detail=str(exc)) from exc

    @app.get("/api/v1/sessions/{session_id}/artifacts/{artifact_id}")
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

    @app.delete("/api/v1/sessions/{session_id}/attachments/{attachment_id}")
    def delete_attachment(session_id: str, attachment_id: str, request: Request) -> dict[str, bool]:
        _require_auth(request, auth)
        try:
            manager.get(session_id).remove_attachment(attachment_id)
            return {"ok": True}
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=exc.status_code or 409, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/close")
    def close_session(session_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return manager.close(session_id)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/archive")
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

    @app.get("/api/v1/sessions/{session_id}/changes")
    def changes(session_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return list_changes(manager.get(session_id).runtime.project_context)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DiffError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/sessions/{session_id}/changes/{path:path}")
    def change(session_id: str, path: str, request: Request) -> dict[str, Any]:
        _require_auth(request, auth)
        try:
            return change_patch(manager.get(session_id).runtime.project_context, path)
        except RuntimeManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DiffError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.websocket("/api/v1/sessions/{session_id}/stream")
    async def stream(websocket: WebSocket, session_id: str) -> None:
        host = websocket.url.hostname
        expected_origin = f"http://{websocket.headers.get('host', '')}"
        if (
            host not in {"127.0.0.1", "localhost", "testserver"}
            or websocket.headers.get("origin") != expected_origin
            or not auth.valid(websocket.cookies.get(COOKIE_NAME))
        ):
            await websocket.close(code=1008)
            return
        try:
            handle = manager.get(session_id)
        except RuntimeManagerError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        subscriber_id, inbox = handle.publisher.subscribe()
        try:
            stream_id = websocket.query_params.get("stream_id")
            last_seq_value = websocket.query_params.get("last_seq")
            try:
                last_seq = int(last_seq_value) if last_seq_value is not None else None
            except ValueError:
                last_seq = None
            replay = handle.publisher.replay(stream_id, last_seq)
            last_sent = last_seq or 0
            if replay is None:
                await websocket.send_json({
                    "type": "snapshot_required",
                    "snapshot": handle.snapshot(),
                })
                last_sent = handle.publisher.latest_seq
            else:
                for event in replay:
                    await websocket.send_json(event.to_dict())
                    last_sent = event.seq

            async def send_events() -> None:
                nonlocal last_sent
                while True:
                    try:
                        event = await asyncio.to_thread(inbox.get, True, 0.5)
                    except queue.Empty:
                        continue
                    if event.seq <= last_sent:
                        continue
                    await websocket.send_json(event.to_dict())
                    last_sent = event.seq

            async def receive_commands() -> None:
                while True:
                    raw = await websocket.receive_text()
                    command: Any = {}
                    try:
                        command = json.loads(raw)
                        if not isinstance(command, dict):
                            raise RuntimeManagerError("command must be a JSON object")
                        command_type = command.get("type")
                        command_id = str(command.get("command_id", ""))
                        if command_type == "turn.submit":
                            attachment_ids = command.get("attachment_ids", [])
                            if not isinstance(attachment_ids, list) or not all(
                                isinstance(item, str) for item in attachment_ids
                            ):
                                raise RuntimeManagerError("attachment_ids must be a string array")
                            handle.submit(
                                str(command.get("prompt", "")), command_id, attachment_ids
                            )
                        elif command_type == "turn.cancel":
                            handle.cancel(command_id)
                        elif command_type == "turn.cancel_queued":
                            handle.cancel_queued(
                                command_id,
                                str(command.get("target_command_id", "")),
                            )
                        elif command_type == "interaction.respond":
                            handle.respond(
                                command_id,
                                str(command.get("request_id", "")),
                                command.get("answer"),
                            )
                        else:
                            raise RuntimeManagerError("unknown command")
                    except (ValueError, RuntimeManagerError) as exc:
                        handle.publisher.publish("command.rejected", {
                            "command_id": str(command.get("command_id", "")) if isinstance(command, dict) else "",
                            "reason": str(exc),
                        })

            sender = asyncio.create_task(send_events())
            receiver = asyncio.create_task(receive_commands())
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        finally:
            handle.publisher.unsubscribe(subscriber_id)

    if assets.is_dir():
        asset_dir = assets / "assets"
        if asset_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=asset_dir), name="assets")

        @app.get("/{path:path}")
        def frontend(path: str) -> FileResponse:
            requested = (assets / path).resolve()
            try:
                requested.relative_to(assets)
            except ValueError:
                requested = assets / "index.html"
            if path and requested.is_file():
                return FileResponse(requested)
            return FileResponse(assets / "index.html")

    return app


def _available_port(requested: int) -> int:
    if requested:
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run_web(args: Any) -> None:
    import uvicorn

    from ..runtime import load_env

    load_env()
    project_root = (args.workspace or Path.cwd()).expanduser().resolve()
    manager = RuntimeManager(
        project_root,
        capacity=args.web_capacity,
        base_args=args,
    )
    auth = BootstrapAuth()
    port = _available_port(args.web_port)
    url = f"http://127.0.0.1:{port}/#bootstrap={auth.bootstrap_token}"
    print(f"Wright Web: {url}")
    if not args.no_open:
        webbrowser.open(url)
    uvicorn.run(
        create_app(manager, auth),
        host="127.0.0.1",
        port=port,
        log_level="info",
    )
