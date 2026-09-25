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

from ...infrastructure.workspace.worktrees import WorktreeError
from ..api import create_api_router
from ..websocket import handle_session_stream
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

    app.include_router(create_api_router(manager, auth))

    @app.websocket("/api/v1/sessions/{session_id}/stream")
    async def stream(websocket: WebSocket, session_id: str) -> None:
        await handle_session_stream(websocket, session_id, manager, auth)

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

    from ...application.runtime import load_env

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
