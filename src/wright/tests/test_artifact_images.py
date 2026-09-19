import base64
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from wright.agent import Agent
from wright.artifacts import ArtifactStore
from wright.checkpoint import SessionCheckpointStore
from wright.renderer import SilentRenderer
from wright.session import Session
from wright.tools.base import Tool, ToolResult
from wright.tools.mcp_client import _to_tool_result

from .test_attachments import _png_bytes
from .test_llm import _client, _reply, _responses_client


@pytest.mark.parametrize("data,media_type", [
    (b"hello", "image/png"),
    (_png_bytes()[:-12], "image/png"),
    (_png_bytes(), "image/jpeg"),
], ids=["not-an-image", "truncated", "wrong-mime"])
def test_mcp_rejects_invalid_or_mislabeled_image_without_storing_it(tmp_path, data, media_type):
    store = ArtifactStore(tmp_path / "artifacts")
    result = _to_tool_result(SimpleNamespace(
        content=[SimpleNamespace(type="image", data=base64.b64encode(data).decode(), mimeType=media_type)],
        isError=False,
    ), artifact_store=store)
    assert not result.artifacts
    assert result.content[0].get("error")
    assert not store.root.exists() or not list(store.root.iterdir())


def test_artifact_rejects_oversized_base64_before_decoding(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(base64, "b64decode", lambda *_a, **_k: pytest.fail("must check size first"))
    with pytest.raises(ValueError, match="size limit"):
        store.register_base64("A" * 100, name="image", media_type="image/png", run_id="", call_id="", max_bytes=4)


def test_foreign_tool_image_reference_is_not_read(tmp_path):
    from wright.model_adapters import ChatAdapter

    adapter = ChatAdapter(
        lambda _: pytest.fail("not an attachment"),
        lambda _: pytest.fail("foreign reference must not be read"),
    )
    messages = adapter.encode_messages([{
        "role": "tool", "tool_call_id": "other-call",
        "content": json.dumps({"artifacts": [{
            "id": "artifact", "media_type": "image/png", "call_id": "original-call",
        }]}),
    }])
    assert "another tool call" in messages[-1]["content"][-1]["text"]


@pytest.mark.parametrize("transport", ["chat", "responses"])
def test_agent_tool_images_reach_provider_and_survive_checkpoint(tmp_path, transport):
    store = ArtifactStore(tmp_path / "artifacts")
    png = _png_bytes()
    encoded = base64.b64encode(png).decode()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        calls = [
            {"id": "image-call", "type": "function", "function": {"name": "report", "arguments": "{}"}},
            {"id": "text-call", "type": "function", "function": {"name": "text", "arguments": "{}"}},
        ] if len(requests) == 1 else []
        if transport == "chat":
            return httpx.Response(200, json=_reply(
                {"content": "" if calls else "seen", "tool_calls": calls},
                finish="tool_calls" if calls else "stop",
            ))
        output = [
            {"type": "function_call", "call_id": call["id"], **call["function"]}
            for call in calls
        ] if calls else [{
            "id": "msg", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "seen", "annotations": []}],
        }]
        return httpx.Response(200, json={
            "id": "resp", "object": "response", "created_at": 0, "model": "test",
            "status": "completed", "output": output,
        })

    def report(_args, runtime):
        return _to_tool_result(SimpleNamespace(
            content=[SimpleNamespace(type="image", data=encoded, mimeType="image/png")],
            structuredContent={"count": 2}, isError=False,
        ), artifact_store=store, run_id=runtime.capabilities.scope.run_id, call_id=runtime.tool_call_id)

    llm = (_client if transport == "chat" else _responses_client)(handler, stream=False)
    llm.artifact_store = store
    try:
        session = Session.create("report", tmp_path)
        agent = Agent(llm, [
            Tool("report", "image report", {"type": "object"}, report),
            Tool("text", "text report", {"type": "object"}, lambda *_: ToolResult.success("text")),
        ], session, SilentRenderer())
        assert agent.run("make and inspect the report") == "seen"
        history = deepcopy(session.wire_messages())

        def assert_image_request(body):
            wire = body["messages" if transport == "chat" else "input"]
            tool_indexes = [i for i, message in enumerate(wire)
                            if message.get("role") == "tool" or message.get("type") == "function_call_output"]
            assert len(tool_indexes) == 2
            # Chat requires every tool result in the batch before any user image.
            image_message = wire[max(tool_indexes) + 1]
            assert image_message["role"] == "user"
            images = [part for part in image_message["content"]
                      if part["type"] in {"image_url", "input_image"}]
            assert len(images) == 1
            image_url = images[0]["image_url"]
            if isinstance(image_url, dict):
                image_url = image_url["url"]
            assert image_url == f"data:image/png;base64,{encoded}"

        assert_image_request(requests[1])
        checkpoint = SessionCheckpointStore(tmp_path / "checkpoints")
        checkpoint.save(session)
        assert encoded not in checkpoint.path_for(session.session_id).read_text()
        restored = checkpoint.load(session.session_id)
        list(llm(restored.wire_messages()))
        assert_image_request(requests[-1])
        assert session.wire_messages() == history

        ref = restored.tool_executions["image-call"].result.artifacts[0]
        store.path_for(ref).unlink()
        list(llm(restored.wire_messages()))
        wire = requests[-1]["messages" if transport == "chat" else "input"]
        assert "unavailable" in json.dumps(wire).lower()
        assert encoded not in json.dumps(wire)
    finally:
        llm.client.close()
