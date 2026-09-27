"""E2E: compaction on a FALLBACK model must not cost the next turn its prompt.

The #1077 property, proved through a real ``GatewayRunner`` + ``AIAgent`` on a
scratch ``HERMES_HOME`` against a local fake provider (unit coverage lives in
``tests/agent/test_compaction_fallback_prompt_identity.py``):

1. human turn on the primary (stores the primary-identity system prompt);
2. human turn: the primary answers 429 -> the configured fallback takes the
   turn -> the fallback answers context-overflow -> compaction runs WHILE the
   fallback is active -> the fallback retry answers;
3. human turn back on the primary.

Assertions: the session row written by the compaction carries the PRIMARY
``Model:``/``Provider:`` lines, turn 3 reuses the stored prompt (no
``stale runtime identity`` rebuild) and the system prompt turn 3 sends on the
wire is byte-identical to the stored row.

The ``pre_1077`` arm is the negative control: it persists the in-memory
(fallback-identity) prompt, as before #1077, and asserts the harness sees the
stale-identity rebuild. If that arm ever passes by accident, the harness no
longer measures the property.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

PRIMARY_MODEL = "primary-e2e-model"
FALLBACK_MODEL = "fallback-e2e-model"

_ORIGIN = dict(
    platform=Platform.DISCORD,
    chat_id="1513247605675790346",
    chat_type="group",
    user_id="117431298246705156",
    scope_id="1480524732964278294",
)


def _openai_ok(stream: bool, text: str = "ok") -> tuple[bytes, str]:
    if not stream:
        return json.dumps({
            "id": "m", "object": "chat.completion", "model": "e2e",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        }).encode(), "application/json"
    chunks = (
        {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                 "finish_reason": None}]},
        {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "m", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                                             "total_tokens": 11}},
    )
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"
    return body, "text/event-stream"


_RATE_LIMIT = (429, {"error": {"message": "Rate limit reached for requests",
                               "type": "rate_limit_error", "code": "rate_limit_exceeded"}})
_OVERFLOW = (400, {"error": {
    "message": "This model's maximum context length is 200000 tokens. However, your "
               "messages resulted in 250000 tokens. Please reduce the length of the messages.",
    "type": "invalid_request_error", "code": "context_length_exceeded"}})


class _Provider(BaseHTTPRequestHandler):
    """Routes by path prefix: /primary, /fallback, /aux.

    ``script[prefix]`` is a queue of error responses served before the
    default 200 answer; every inference request is recorded.
    """

    script: dict = {}
    calls: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        prefix = self.path.split("/")[1]
        req = json.loads(raw.decode() or "{}")
        queue = type(self).script.get(prefix) or []
        type(self).calls.append((prefix, self.path, req, "err" if queue else "ok"))
        if queue:
            status, err = queue.pop(0)
            payload, ctype = json.dumps(err).encode(), "application/json"
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "0")
        else:
            text = "summary of the earlier turns" if prefix == "aux" else "ok"
            payload, ctype = _openai_ok(bool(req.get("stream")), text)
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def provider():
    _Provider.script = {}
    _Provider.calls = []
    srv = HTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _Provider
    finally:
        srv.shutdown()
        srv.server_close()


def _runner(tmp_path, monkeypatch, url: str):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n"
        f"  default: {PRIMARY_MODEL}\n"
        "  provider: custom\n"
        f"  base_url: {url}/primary/v1\n"
        "fallback_providers:\n"
        "  - provider: custom\n"
        f"    model: {FALLBACK_MODEL}\n"
        f"    base_url: {url}/fallback/v1\n"
        "    api_key: test-key\n"
        "compression:\n"
        "  enabled: true\n"
        "auxiliary:\n"
        "  title_generation:\n"
        "    enabled: false\n"
        "  compression:\n"
        "    provider: custom\n"
        "    model: aux-e2e-model\n"
        f"    base_url: {url}/aux/v1\n"
        "    api_key: test-key\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    import agent.model_metadata as mm
    import gateway.run as gr
    import gateway.session as gs

    runtime = {"api_key": "test-key", "base_url": f"{url}/primary/v1",
               "provider": "custom", "api_mode": "chat_completions"}
    monkeypatch.setattr(gs, "_discord_tools_loaded", lambda: True)
    # gateway.run snapshots the home at import; the fallback chain is read
    # through it, so point it at this test's scratch home.
    monkeypatch.setattr(gr, "_hermes_home", home)
    monkeypatch.setattr(gr, "_resolve_runtime_agent_kwargs", lambda: dict(runtime))
    monkeypatch.setattr(mm, "get_model_context_length", lambda *a, **k: 200_000)

    runner = gr.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._is_user_authorized = lambda source, **_kw: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    return runner


async def _human_turn(runner, message_id: str, text: str):
    src = SessionSource(**_ORIGIN, chat_name="Guild / #general", user_name="Ace",
                        message_id=message_id)
    return await runner._handle_message(MessageEvent(text=text, source=src,
                                                     message_id=message_id))


def _restore_pre_1077(monkeypatch):
    """Negative control: persist the in-memory (fallback-identity) prompt."""
    import agent.chat_completion_helpers as cch

    monkeypatch.setattr(cch, "prompt_for_persistence", lambda agent, prompt: prompt)


def _identity(prompt: str) -> tuple[str, str]:
    lines = prompt.splitlines()
    model = [ln for ln in lines if ln.startswith("Model: ")][-1]
    provider = [ln for ln in lines if ln.startswith("Provider: ")][-1]
    return model, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["head", "pre_1077"])
async def test_fallback_compaction_keeps_primary_prompt_for_next_turn(
    tmp_path, monkeypatch, provider, caplog, variant
):
    url, prov = provider
    runner = _runner(tmp_path, monkeypatch, url)
    if variant == "pre_1077":
        _restore_pre_1077(monkeypatch)

    # Turn 1: primary answers. Pad history so compaction has a middle to fold.
    replies = [await _human_turn(runner, "1552671843494666330", "hi")]
    for i in range(4):
        replies.append(await _human_turn(runner, f"15526718434946663{40 + i}",
                                         f"note {i}: " + "filler sentence. " * 600))

    # Turn 2: primary 429 -> fallback; fallback overflows once -> compaction
    # runs with the fallback active -> fallback retry answers.
    prov.script = {"primary": [_RATE_LIMIT] * 8, "fallback": [_OVERFLOW]}
    mark = len(prov.calls)
    replies.append(await _human_turn(runner, "1552671843494666331", "again"))
    turn2 = prov.calls[mark:]
    prov.script = {}

    # Sanity: the scenario really happened (else the ratchet is vacuous).
    prefixes = [c[0] for c in turn2]
    assert "fallback" in prefixes, prefixes
    assert "aux" in prefixes, f"no compaction summary call during turn 2: {prefixes}"
    assert turn2[-1][0] == "fallback" and turn2[-1][3] == "ok", prefixes

    # The runner holds an async facade; read the row through the sync DB.
    db = getattr(runner._session_db, "_db", runner._session_db)
    session_key = next(iter(runner.session_store._entries))
    session_id = runner.session_store._entries[session_key].session_id
    stored = db.get_session(session_id)["system_prompt"]
    assert stored, "compaction did not persist a system prompt"

    # Turn 3: back on the primary.
    mark = len(prov.calls)
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        replies.append(await _human_turn(runner, "1552671843494666332", "thanks"))
    # Inference requests only; metadata probes (/api/show) share the base URL.
    turn3 = [c for c in prov.calls[mark:]
             if c[0] != "aux" and c[1].endswith("/chat/completions")]
    assert all(r == "ok" for r in replies), replies
    assert [c[0] for c in turn3] == ["primary"], [c[1] for c in turn3]
    stale = [r.getMessage() for r in caplog.records
             if "stale runtime identity" in r.getMessage()]

    if variant == "pre_1077":
        assert _identity(stored)[0] == f"Model: {FALLBACK_MODEL}", _identity(stored)
        assert len(stale) == 1, stale
        return

    assert _identity(stored) == (f"Model: {PRIMARY_MODEL}", "Provider: custom"), _identity(stored)
    assert stale == [], stale
    wire_system = turn3[0][2]["messages"][0]
    assert wire_system["role"] == "system"
    # The gateway appends the pinned session-context block after the stored
    # prompt, so the stored row must be a byte prefix of what is sent.
    assert wire_system["content"].startswith(stored), (
        "turn 3 did not reuse the stored prompt bytes"
    )
    # And the primary's prompt is the same one the session sent before the
    # fallback turn: compaction on the fallback cost it nothing.
    turn1_system = next(
        c[2]["messages"][0] for c in prov.calls
        if c[0] == "primary" and c[1].endswith("/chat/completions") and c[3] == "ok"
    )
    assert wire_system == turn1_system, "primary system prompt changed across the fallback compaction"
