"""WebSocket connection handling and real-time event streaming."""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..web.auth import BootstrapAuth
from ..web.runtime_manager import RuntimeManager, RuntimeManagerError

COOKIE_NAME = "wright_web_session"


async def handle_session_stream(
    websocket: WebSocket,
    session_id: str,
    manager: RuntimeManager,
    auth: BootstrapAuth,
) -> None:
    """Handle real-time bidirectional WebSocket event stream for a session."""
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
