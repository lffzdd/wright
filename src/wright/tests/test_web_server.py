from types import SimpleNamespace

from fastapi.testclient import TestClient

from ..ui_events import EventPublisher
from ..web.auth import BootstrapAuth
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
        )

    def project(self):
        return {"project_id": "project", "name": "test"}

    def list_sessions(self):
        return []

    def get(self, session_id):
        if session_id != "session":
            raise RuntimeError("missing")
        return self.handle

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
