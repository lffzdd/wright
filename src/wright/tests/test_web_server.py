from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ..tools.base import ArtifactRef
from ..ui_events import EventPublisher
from ..web.auth import BootstrapAuth
from ..web.runtime_manager import RuntimeManagerError
from ..web.server import COOKIE_NAME, create_app


class FakeManager:
    def __init__(self):
        publisher = EventPublisher(project_id="project", session_id="session")
        publisher.publish("system.notice", {"text": "ready"})
        self.handle = SimpleNamespace(
            publisher=publisher,
            snapshot=lambda: {
                "stream_id": publisher.stream_id,
                "last_seq": publisher.latest_seq,
            },
            upload_attachment=lambda filename, data: {
                "id": "att_test", "filename": filename, "media_type": "image/png",
                "size": len(data), "sha256": "a" * 64, "width": 1, "height": 1,
                "storage_path": "session/att_test.png",
            },
            remove_attachment=lambda _attachment_id: None,
            command_status=lambda command_id: {
                "command_id": command_id, "status": "completed", "result": {"run_id": "run-1"},
            },
        )

    def project(self):
        return {"project_id": "project", "name": "test"}

    def list_sessions(self):
        return []

    def get(self, session_id):
        if session_id != "session":
            raise RuntimeError("missing")
        return self.handle

    def set_model(self, session_id, model):
        if session_id != "session":
            raise RuntimeManagerError(
                f"active session not found: {session_id}",
                status_code=404,
            )
        cleaned = str(model).strip()
        if not cleaned:
            raise RuntimeManagerError("model name cannot be empty")
        return {"session_id": session_id, "model": cleaned}

    def shutdown(self):
        pass


def _authenticated_client(tmp_path):
    static = tmp_path / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<main>Wright</main>")
    auth = BootstrapAuth("bootstrap-secret")
    client = TestClient(create_app(FakeManager(), auth, static_dir=static))
    response = client.post(
        "/api/v1/auth/exchange",
        json={"token": "bootstrap-secret"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert COOKIE_NAME in client.cookies
    return client, auth


def test_bootstrap_token_is_single_use_and_cookie_is_http_only(tmp_path):
    client, _auth = _authenticated_client(tmp_path)

    second = client.post(
        "/api/v1/auth/exchange",
        json={"token": "bootstrap-secret"},
        headers={"origin": "http://testserver"},
    )

    assert second.status_code == 401
    original = client.cookies.get(COOKIE_NAME)
    assert original


def test_api_requires_cookie_origin_and_sets_security_headers(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("Wright")
    app = create_app(FakeManager(), BootstrapAuth("secret"), static_dir=static)
    anonymous = TestClient(app)

    assert anonymous.get("/api/v1/project").status_code == 401
    assert anonymous.post(
        "/api/v1/auth/exchange", json={"token": "secret"}
    ).status_code == 403
    bad_host = anonymous.get(
        "/api/v1/health", headers={"host": "example.com"}
    )
    assert bad_host.status_code == 400

    client, _auth = _authenticated_client(tmp_path / "other")
    response = client.get("/api/v1/project")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_websocket_replays_events_after_authenticated_connect(tmp_path):
    client, _auth = _authenticated_client(tmp_path)
    # FakeManager creates one retained notice. A client at seq 0 receives it.
    with client.websocket_connect(
        "/api/v1/sessions/session/stream?last_seq=0",
        headers={"origin": "http://testserver"},
    ) as websocket:
        event = websocket.receive_json()
        assert event["type"] == "system.notice"
        assert event["seq"] == 1


def test_set_model_endpoint(tmp_path):
    client, _auth = _authenticated_client(tmp_path)
    response = client.post(
        "/api/v1/sessions/session/model",
        json={"model": "gpt-4o-mini"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert response.json()["model"] == "gpt-4o-mini"


def test_command_status_endpoint_allows_reconnect_query(tmp_path):
    client, _auth = _authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/sessions/session/commands/cmd-1",
        headers={"origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"run_id": "run-1"}


def test_set_model_missing_session_is_404(tmp_path):
    client, _auth = _authenticated_client(tmp_path)
    response = client.post(
        "/api/v1/sessions/missing/model",
        json={"model": "gpt-4o-mini"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_set_model_empty_name_is_409(tmp_path):
    client, _auth = _authenticated_client(tmp_path)
    response = client.post(
        "/api/v1/sessions/session/model",
        json={"model": "   "},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 409
    assert "cannot be empty" in response.json()["detail"]


def test_authenticated_attachment_upload_and_delete(tmp_path):
    client, _auth = _authenticated_client(tmp_path)

    uploaded = client.post(
        "/api/v1/sessions/session/attachments",
        files={"file": ("image.png", b"png-bytes", "image/png")},
        headers={"origin": "http://testserver"},
    )
    removed = client.delete(
        "/api/v1/sessions/session/attachments/att_test",
        headers={"origin": "http://testserver"},
    )

    assert uploaded.status_code == 200
    assert uploaded.json()["filename"] == "image.png"
    assert removed.status_code == 200


def test_authenticated_artifact_delivery_uses_registered_reference(tmp_path):
    artifact = tmp_path / "report.md"
    artifact.write_text("# Report\ncount: 2", encoding="utf-8")
    # The route resolves through the handle, never from an arbitrary path sent
    # by the browser.  Install the test-only registered reference on the same
    # manager used to create the application.
    # TestClient intentionally does not expose its app manager, so build a
    # second authenticated client with the reference-bearing handle.
    static = tmp_path / "artifact-static"
    static.mkdir()
    (static / "index.html").write_text("Wright", encoding="utf-8")
    auth = BootstrapAuth("artifact-secret")
    fake = FakeManager()
    fake.handle.artifact_path = lambda artifact_id: (
        ArtifactRef(artifact_id, "text/markdown", "report.md", artifact.stat().st_size),
        Path(artifact),
    )
    artifact_client = TestClient(create_app(fake, auth, static_dir=static))
    artifact_client.post(
        "/api/v1/auth/exchange", json={"token": "artifact-secret"},
        headers={"origin": "http://testserver"},
    )
    response = artifact_client.get("/api/v1/sessions/session/artifacts/artifact-report")

    assert response.status_code == 200
    assert response.content == b"# Report\ncount: 2"
    assert response.headers["content-type"].startswith("text/markdown")
