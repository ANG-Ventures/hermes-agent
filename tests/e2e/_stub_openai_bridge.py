"""Minimal OpenAI-shape ``/v1/chat/completions`` stub that records every body.

Used by ``test_interrupt_close_owns_transcript_e2e.py``. Behaviour:

* every POST body is appended (as parsed JSON) to ``self.bodies``;
* when the newest user message contains ``RUN_TOOL:<command>`` and no tool
  result follows it yet, the stub answers with ONE ``terminal`` tool call
  running ``<command>``;
* anything else gets a fixed text completion (``STUB_OK``).

Both streaming (SSE) and non-streaming responses are served, so the stub does
not depend on which transport the harness picks for a chat_completions lane.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIXED_TEXT = "STUB_OK"
TOOL_CALL_ID = "call_stub_1"


def _wants_tool(messages) -> str | None:
    # Only the FIRST request of a conversation (no assistant/tool row yet) gets
    # a tool call: after a restart the harness may merge user rows, and a
    # re-issued tool call would make the resume turn block on the tool again.
    if not isinstance(messages, list):
        return None
    if any(m.get("role") in ("assistant", "tool") for m in messages if isinstance(m, dict)):
        return None
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "user":
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            content = content or ""
            if "RUN_TOOL:" in content:
                return content.split("RUN_TOOL:", 1)[1].strip()
            return None
    return None


class StubBridge:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):  # quiet
                return

            def _send_json(self, code: int, obj: dict) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # /v1/models probes
                self._send_json(200, {"object": "list", "data": [
                    {"id": "stub-model", "object": "model", "owned_by": "stub"}
                ]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {"_unparsed": raw.decode("utf-8", "replace")}
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self._send_json(404, {"error": {"message": "stub: unknown path"}})
                    return
                with stub._lock:
                    stub.bodies.append(body)
                command = _wants_tool(body.get("messages") or [])
                model = body.get("model") or "stub-model"
                if body.get("stream"):
                    self._stream(model, command)
                else:
                    self._send_json(200, self._completion(model, command))

            def _completion(self, model: str, command: str | None) -> dict:
                if command is not None:
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": TOOL_CALL_ID,
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": command}),
                            },
                        }],
                    }
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": FIXED_TEXT}
                    finish = "stop"
                return {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

            def _stream(self, model: str, command: str | None) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def chunk(delta: dict, finish=None, usage=None) -> None:
                    obj = {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    if usage is not None:
                        obj["usage"] = usage
                    self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")

                if command is not None:
                    chunk({"role": "assistant", "content": None, "tool_calls": [{
                        "index": 0,
                        "id": TOOL_CALL_ID,
                        "type": "function",
                        "function": {"name": "terminal",
                                     "arguments": json.dumps({"command": command})},
                    }]})
                    chunk({}, finish="tool_calls",
                          usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                else:
                    chunk({"role": "assistant", "content": FIXED_TEXT})
                    chunk({}, finish="stop",
                          usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> "StubBridge":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.bodies)
