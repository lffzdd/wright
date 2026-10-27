"""Scripted OpenAI-compatible Chat Completions server for end-to-end tests.

Run: ``python fake_llm.py PORT`` with ``FAKE_LLM_QUEUE`` (a JSON file holding a
list of scripted replies, consumed in order) and ``FAKE_LLM_LOG`` (a JSONL file
that receives every request body and the reply served for it).

A scripted reply is ``{"content", "reasoning", "tool_calls", "finish_reason",
"quirk"}``; ``tool_calls`` items are ``{"id", "name", "arguments"}``.

Quirks reproduce what real OpenAI-compatible servers do:

- ``http400``: reject the request with a provider error body.
- ``slow``: stream text slowly, so a test can press Stop mid-answer.
- ``drop_mid_stream``: close the connection after a few chunks.
- ``repeat_id_name``: resend the call id and name on every fragment.
- ``no_index``: omit ``index`` from tool-call fragments.
- ``no_finish``: never send a finish reason.

Requests without tools (Wright's memory selector) get a neutral JSON reply
and do not consume the queue, unless the next scripted reply sets ``aux``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

QUEUE = Path(os.environ.get("FAKE_LLM_QUEUE", "queue.json"))
LOG = Path(os.environ.get("FAKE_LLM_LOG", "requests.jsonl"))
_LOCK = threading.Lock()
_AUX_REPLY = {
    "content": json.dumps({"selected_memories": [], "selected_episodes": []}),
    "finish_reason": "stop",
}


def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> bytes:
    body: dict = {
        "id": "fake", "object": "chat.completion.chunk", "created": 0, "model": "fake",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        body["choices"] = []
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n".encode()


def _next_reply(request: dict) -> dict:
    with _LOCK:
        queue = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
        if not request.get("tools") and not (queue and queue[0].get("aux")):
            reply = _AUX_REPLY
        else:
            reply = queue.pop(0) if queue else {"content": "SCRIPT EXHAUSTED", "finish_reason": "stop"}
            QUEUE.write_text(json.dumps(queue))
        with LOG.open("a") as handle:
            handle.write(json.dumps(request) + "\n")
            handle.write(json.dumps({"_served": reply}) + "\n")
    return reply


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = _next_reply(request)
        quirk = reply.get("quirk", "")
        if quirk == "http400":
            self._json(400, {"error": {"message": "fake bad request", "type": "invalid_request_error"}})
            return
        finish = reply.get("finish_reason", "tool_calls" if reply.get("tool_calls") else "stop")
        if not request.get("stream"):
            message: dict = {"role": "assistant", "content": reply.get("content") or None}
            if reply.get("tool_calls"):
                message["tool_calls"] = [
                    {"id": call["id"], "type": "function",
                     "function": {"name": call["name"], "arguments": call.get("arguments", "{}")}}
                    for call in reply["tool_calls"]
                ]
            self._json(200, {
                "id": "fake", "object": "chat.completion", "created": 0, "model": "fake",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        write = self.wfile.write
        if reply.get("reasoning"):
            write(_chunk({"role": "assistant", "reasoning_content": reply["reasoning"]}))
        text = reply.get("content", "")
        for start in range(0, len(text), 8):
            write(_chunk({"content": text[start:start + 8]}))
            self.wfile.flush()
            if quirk == "slow":
                time.sleep(0.4)
            if quirk == "drop_mid_stream" and start >= 8:
                self.connection.shutdown(2)
                return
        for position, call in enumerate(reply.get("tool_calls", [])):
            arguments = call.get("arguments", "{}")
            index = {} if quirk == "no_index" else {"index": position}
            write(_chunk({"tool_calls": [{**index, "id": call["id"], "type": "function",
                                          "function": {"name": call["name"], "arguments": ""}}]}))
            for start in range(0, max(len(arguments), 1), 5):
                piece = arguments[start:start + 5]
                fragment = {**index, "function": {"arguments": piece}}
                if quirk == "repeat_id_name":
                    fragment = {**index, "id": call["id"], "type": "function",
                                "function": {"name": call["name"], "arguments": piece}}
                write(_chunk({"tool_calls": [fragment]}))
        write(_chunk({}, None if quirk == "no_finish" else finish))
        write(_chunk({}, usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}))
        write(b"data: [DONE]\n\n")

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
