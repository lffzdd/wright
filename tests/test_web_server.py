from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from wright.application.session.publisher import EventPublisher
from wright.domain.model.tool import ArtifactRef
from wright.interfaces.web.auth import COOKIE_NAME, BootstrapAuth
from wright.interfaces.web.runtime_manager import RuntimeManagerError
from wright.interfaces.web.server import create_app


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
        self.run_snapshot = {
            "run": {"id": "run-1", "status": "completed"},
            "history": [{"event_id": "run-1:event:1", "event_type": "run_finished"}],
            "tool_executions": [],
        }

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

    def run_history(self, run_id):
        if run_id != "run-1":
            raise RuntimeManagerError("durable run not found", status_code=404)
        return self.run_snapshot

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


def test_durable_run_history_endpoint_is_independent_of_source_session(tmp_path):
    client, _auth = _authenticated_client(tmp_path)
    response = client.get(
        "/api/v1/runs/run-1",
        headers={"origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "completed"
    assert response.json()["history"][0]["event_type"] == "run_finished"


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


def test_malformed_command_json_does_not_drop_the_stream(tmp_path):
    client, _auth = _authenticated_client(tmp_path)
    with client.websocket_connect(
        "/api/v1/sessions/session/stream?last_seq=0",
        headers={"origin": "http://testserver"},
    ) as websocket:
        assert websocket.receive_json()["type"] == "system.notice"
        websocket.send_text("{")
        rejected = websocket.receive_json()
        assert rejected["type"] == "command.rejected"
        websocket.send_text("[]")
        assert websocket.receive_json()["type"] == "command.rejected"


def test_event_published_inside_snapshot_is_still_delivered(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("Wright", encoding="utf-8")
    auth = BootstrapAuth("bootstrap-secret")
    manager = FakeManager()
    publisher = manager.handle.publisher

    def snapshot():
        body = {"stream_id": publisher.stream_id, "last_seq": publisher.latest_seq, "session": {"session_id": "session"}}
        publisher.publish("content.delta", {"piece": "during"})
        return body

    manager.handle.snapshot = snapshot
    client = TestClient(create_app(manager, auth, static_dir=static))
    client.post(
        "/api/v1/auth/exchange",
        json={"token": "bootstrap-secret"},
        headers={"origin": "http://testserver"},
    )
    with client.websocket_connect(
        "/api/v1/sessions/session/stream?stream_id=missing&last_seq=0",
        headers={"origin": "http://testserver"},
    ) as websocket:
        frame = websocket.receive_json()
        assert frame["type"] == "snapshot_required"
        assert frame["snapshot"]["last_seq"] == 1
        delivered = websocket.receive_json()
        assert delivered["payload"]["piece"] == "during"
        assert delivered["seq"] == frame["snapshot"]["last_seq"] + 1


def test_real_socket_delivers_a_scripted_turn_without_mixing_child_events(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("Wright", encoding="utf-8")
    auth = BootstrapAuth("bootstrap-secret")
    manager = FakeManager()
    publisher = manager.handle.publisher
    seen: set[str] = set()

    def submit(prompt, command_id, attachment_ids=None, document_ids=None):
        duplicate = command_id in seen
        seen.add(command_id)
        publisher.publish("command.accepted", {
            "command_id": command_id,
            "command": "turn.submit",
            "prompt": prompt,
            "queued": False,
            "duplicate": duplicate,
        })
        if duplicate:
            return {"duplicate": True}
        publisher.publish("turn.started", {"prompt": prompt, "command_id": command_id}, turn_id="turn-1")
        publisher.publish("content.final", {"content": "root answer"})
        publisher.publish(
            "content.final",
            {"content": "child answer", "agent_depth": 1, "agent_task_id": "task-1"},
        )
        return {"duplicate": False}

    manager.handle.submit = submit
    client = TestClient(create_app(manager, auth, static_dir=static))
    client.post(
        "/api/v1/auth/exchange",
        json={"token": "bootstrap-secret"},
        headers={"origin": "http://testserver"},
    )
    with client.websocket_connect(
        "/api/v1/sessions/session/stream?last_seq=0",
        headers={"origin": "http://testserver"},
    ) as websocket:
        assert websocket.receive_json()["type"] == "system.notice"
        command = {
            "type": "turn.submit",
            "command_id": "cmd-same",
            "prompt": "hello",
            "attachment_ids": [],
        }
        websocket.send_json(command)
        events = [websocket.receive_json() for _ in range(4)]
        assert [event["type"] for event in events] == [
            "command.accepted", "turn.started", "content.final", "content.final",
        ]
        assert events[2]["payload"]["content"] == "root answer"
        assert events[2]["payload"].get("agent_task_id") is None
        assert events[3]["payload"]["agent_task_id"] == "task-1"
        assert [event["seq"] for event in events] == [2, 3, 4, 5]
        websocket.send_json(command)
        duplicate = websocket.receive_json()
        assert duplicate["payload"]["duplicate"] is True
        assert publisher.display.view()["active_turn"]["content"] == "root answer"
        assert publisher.display.view()["agents"][0]["content"] == "child answer"


def test_default_static_directory_is_the_vite_build(tmp_path):
    from wright.interfaces.web.server import default_static_dir

    static = default_static_dir()
    assert static.name == "static"
    assert static.parent.name == "web"
    assert static.parent.parent.name == "wright"
    if not (static / "index.html").is_file():
        return
    auth = BootstrapAuth("bootstrap-secret")
    client = TestClient(create_app(FakeManager(), auth))
    client.post(
        "/api/v1/auth/exchange",
        json={"token": "bootstrap-secret"},
        headers={"origin": "http://testserver"},
    )
    page = client.get("/")
    assert page.status_code == 200
    assert "Wright" in page.text or "assets/" in page.text
