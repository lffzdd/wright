"""WebSocket connection handling and real-time event streaming."""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .auth import COOKIE_NAME, BootstrapAuth
from .runtime_manager import RuntimeManager, RuntimeManagerError


def opening_frames(
    handle: Any,
    stream_id: str | None,
    last_seq: int | None,
) -> tuple[list[dict[str, Any]], int]:
    """Replay from the requested cursor, or one snapshot at its own watermark.

    The cursor is the snapshot's ``last_seq`` or the last replayed event.
    It is not ``latest_seq`` read after the snapshot has been sent.
    """

    replay = handle.publisher.replay(stream_id, last_seq)
    if replay is None:
        snapshot = handle.snapshot()
        return [{"type": "snapshot_required", "snapshot": snapshot}], int(snapshot["last_seq"])
    frames = [event.to_dict() for event in replay]
    cursor = replay[-1].seq if replay else (last_seq or 0)
    return frames, cursor


async def send_snapshot(websocket: WebSocket, handle: Any) -> int:
    snapshot = handle.snapshot()
    await websocket.send_json({"type": "snapshot_required", "snapshot": snapshot})
    return int(snapshot["last_seq"])


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
        opening, last_sent = opening_frames(handle, stream_id, last_seq)
        for frame in opening:
            await websocket.send_json(frame)

        async def send_events() -> None:
            nonlocal last_sent
            while True:
                if handle.publisher.take_stale(subscriber_id):
                    last_sent = await send_snapshot(websocket, handle)
                    continue
                try:
                    event = await asyncio.to_thread(inbox.get, True, 0.5)
                except queue.Empty:
                    continue
                if event.seq <= last_sent:
                    continue
                if event.seq != last_sent + 1:
                    last_sent = await send_snapshot(websocket, handle)
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
