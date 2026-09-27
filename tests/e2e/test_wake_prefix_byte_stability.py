"""E2E: human -> kanban wake -> human sends ONE context prompt on the wire.

The #984/#1045 property ("human->wake->human = 1 context prompt"), proved at
the transport instead of at a helper or a stubbed ``_run_agent``:

* a real ``GatewayRunner`` on a scratch ``HERMES_HOME`` handles every turn;
* the wake is produced by the production ``gateway.wake.deliver_wake`` push
  path (``internal=True`` event, source rebuilt from the persisted origin, so
  no chat_name / user_name / message_id);
* a real ``AIAgent`` builds each request and sends it over HTTP to a local
  fake provider, which records the raw request bodies.

The assertions are on those bodies: the system prompt, messages[0] and tools[]
are byte-identical across the three requests, and every request's messages
are a strict prefix of the next one's (the cached prefix is only ever
extended). Both wire shapes are covered: ``chat_completions`` (system prompt
travels as messages[0]) and ``anthropic_messages`` (the apr/apx lane: system
prompt in ``system``, first user turn in messages[0]).

The ``pre_984`` arm is the permanent negative control: it restores the
pre-#984 behaviour (internal events re-render and re-key the session-context
pin) and asserts the harness sees exactly two context prompts (A->B->A). If
that arm ever goes green-by-accident, the harness no longer measures the
property.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

_ORIGIN = dict(
    platform=Platform.DISCORD,
    chat_id="1513247605675790346",
    chat_type="group",
    user_id="117431298246705156",
    scope_id="1480524732964278294",
)
_CHAT_NAME = "Guild / #general"


def _canon(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode()


def _cache_view(value):
    """What the provider's prefix cache hashes, not how it is spelled.

    The anthropic adapter moves a rolling ``cache_control`` breakpoint onto the
    newest user turn, so that turn is sent as a block list on one request and
    as a plain string on the next. Anthropic treats a string as a single text
    block and does not hash the ``cache_control`` marker, so both spellings are
    the same cached prefix. Every other byte (text included) must match.
    """
    if isinstance(value, dict):
        out = {k: _cache_view(v) for k, v in value.items() if k != "cache_control"}
        if isinstance(out.get("content"), str) and "role" in out:
            out["content"] = [{"type": "text", "text": out["content"]}]
        return out
    if isinstance(value, list):
        return [_cache_view(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Fake provider: records raw request bodies, answers both wire shapes.
# ---------------------------------------------------------------------------


def _anthropic_sse() -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_e2e", "type": "message", "role": "assistant", "content": [],
            "model": "claude-e2e", "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def _anthropic_json() -> bytes:
    return json.dumps({
        "id": "msg_e2e", "type": "message", "role": "assistant", "model": "claude-e2e",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }).encode()


def _openai_sse() -> bytes:
    chunks = (
        {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                                 "finish_reason": None}]},
        {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "m", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                                             "total_tokens": 11}},
    )
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def _openai_json() -> bytes:
    return json.dumps({
        "id": "m", "object": "chat.completion", "model": "e2e-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
    }).encode()


class _Provider(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).bodies.append((self.path, raw))
        req = json.loads(raw.decode())
        anthropic = self.path.rstrip("/").endswith("/messages")
        if req.get("stream"):
            payload, ctype = (_anthropic_sse() if anthropic else _openai_sse()), "text/event-stream"
        else:
            payload, ctype = (_anthropic_json() if anthropic else _openai_json()), "application/json"
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
    _Provider.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _Provider.bodies
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Gateway on a scratch HERMES_HOME
# ---------------------------------------------------------------------------

_WIRES = {
    # api_mode -> (runtime kwargs builder, request path suffix)
    "chat_completions": (
        lambda url: {"api_key": "test-key", "base_url": f"{url}/v1",
                     "provider": "custom", "api_mode": "chat_completions"},
        "/chat/completions",
        "e2e-model",
    ),
    "anthropic_messages": (
        lambda url: {"api_key": "test-key", "base_url": url,
                     "provider": "anthropic", "api_mode": "anthropic_messages"},
        "/messages",
        "claude-e2e-model",
    ),
}


def _runner(tmp_path, monkeypatch, runtime: dict, model: str):
    home = tmp_path / ".hermes"
    home.mkdir()
    # A configured primary model is load-bearing: without model.default the
    # gateway reads the agent's model as a fallback activation after every
    # turn and evicts the cached agent (and with it the session-context pin),
    # which no production gateway does. Title generation is disabled so the
    # auxiliary client never leaves the scratch home for a live endpoint.
    (home / "config.yaml").write_text(
        "model:\n"
        f"  default: {model}\n"
        f"  provider: {runtime['provider']}\n"
        "auxiliary:\n"
        "  title_generation:\n"
        "    enabled: false\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    import agent.model_metadata as mm
    import gateway.run as gr
    import gateway.session as gs

    monkeypatch.setattr(gs, "_discord_tools_loaded", lambda: True)
    # gateway.run snapshots the home at import and reads config.yaml through
    # it; without this the scratch model.default is never seen (CI: "No model
    # configured" -> every turn reads as fallback -> evict).
    monkeypatch.setattr(gr, "_hermes_home", home)
    monkeypatch.setattr(gr, "_resolve_runtime_agent_kwargs", lambda: dict(runtime))
    monkeypatch.setattr(mm, "get_model_context_length", lambda *a, **k: 200_000)

    runner = gr.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._is_user_authorized = lambda source, **_kw: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    return runner


def _human(message_id: str) -> SessionSource:
    return SessionSource(**_ORIGIN, chat_name=_CHAT_NAME, user_name="Ace",
                         message_id=message_id)


def _wake_source() -> SessionSource:
    # kanban_watchers._push_wake shape: rebuilt from the persisted
    # subscription origin — no chat_name / user_name / message_id.
    return SessionSource(**_ORIGIN)


class _PushAdapter:
    """Push-capable adapter whose inbound path is the runner's real handler."""

    supports_async_delivery = True

    def __init__(self, runner):
        self._runner = runner
        self.replies: list = []

    async def handle_message(self, event: MessageEvent):
        self.replies.append(await self._runner._handle_message(event))


async def _human_turn(runner, message_id: str, text: str):
    src = _human(message_id)
    return await runner._handle_message(
        MessageEvent(text=text, source=src, message_id=message_id)
    )


async def _drive_human_wake_human(runner):
    from gateway.wake import deliver_wake

    replies = [await _human_turn(runner, "1552671843494666330", "hi")]
    adapter = _PushAdapter(runner)
    await deliver_wake(
        adapter,
        text="[kanban] t_e2e0001 completed: build finished",
        source=_wake_source(),
    )
    replies.extend(adapter.replies)
    replies.append(await _human_turn(runner, "1552671843494666331", "thanks"))
    return replies


def _prefix_parts(api_mode: str, body: dict) -> dict:
    """The parts of a request that must never change once sent."""
    if api_mode == "anthropic_messages":
        system = body.get("system")
    else:
        assert body["messages"][0]["role"] == "system"
        system = body["messages"][0]
    return {
        "system": _canon(_cache_view(system)),
        "msg0": _canon(_cache_view(body["messages"][0])),
        "tools": _canon(_cache_view(body.get("tools"))),
    }


def _restore_pre_984(monkeypatch):
    """Negative control: internal events re-render/re-key the pin (pre-#984)."""
    import gateway.run as gr

    real = gr.GatewayRunner._pinned_session_context_prompt

    def pre_984(self, context, redact_pii, session_key, *, internal=False):
        return real(self, context, redact_pii, session_key, internal=False)

    monkeypatch.setattr(gr.GatewayRunner, "_pinned_session_context_prompt", pre_984)


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", sorted(_WIRES))
@pytest.mark.parametrize("variant", ["head", "pre_984"])
async def test_human_wake_human_sends_one_context_prompt(
    tmp_path, monkeypatch, provider, api_mode, variant
):
    url, bodies = provider
    runtime_for, path_suffix, model = _WIRES[api_mode]
    runner = _runner(tmp_path, monkeypatch, runtime_for(url), model)
    if variant == "pre_984":
        _restore_pre_984(monkeypatch)

    evictions: list = []
    import gateway.run as gr

    _real_evict = gr.GatewayRunner._evict_cached_agent

    def _count_evict(self, session_key):
        evictions.append(session_key)
        return _real_evict(self, session_key)

    monkeypatch.setattr(gr.GatewayRunner, "_evict_cached_agent", _count_evict)

    replies = await _drive_human_wake_human(runner)
    # An eviction resets the pin and makes the ratchet vacuous in both
    # directions; the harness must reuse one cached agent like production.
    assert evictions == [], f"cached agent evicted mid-sequence: {len(evictions)}x"

    assert replies == ["ok", "ok", "ok"], f"not every turn completed: {replies!r}"
    # Only inference requests count; metadata probes (e.g. /api/show) that
    # share the base URL are not part of the conversation prefix.
    sent = [raw for p, raw in bodies if p.endswith(path_suffix)]
    assert len(sent) == 3, [p for p, _ in bodies]
    reqs = [json.loads(raw) for raw in sent]
    parts = [_prefix_parts(api_mode, r) for r in reqs]

    # Sanity: the wire really carries the human-rendered session context, and
    # tools were sent (an empty tools[] would make that assertion vacuous).
    assert _CHAT_NAME.encode() in parts[0]["system"]
    assert reqs[0].get("tools"), "no tools[] on the wire"

    distinct_prompts = len({p["system"] for p in parts})
    if variant == "pre_984":
        # A -> B -> A: the wake re-rendered from the degraded source and the
        # next human turn re-keyed back. The harness must see it.
        assert distinct_prompts == 2, distinct_prompts
        assert parts[0]["system"] == parts[2]["system"] != parts[1]["system"]
        return

    # Ratchet: human -> wake -> human = exactly ONE context prompt.
    assert distinct_prompts == 1, "wake turn rewrote the system prompt"
    assert parts[0]["msg0"] == parts[1]["msg0"] == parts[2]["msg0"], "messages[0] drifted"
    assert parts[0]["tools"] == parts[1]["tools"] == parts[2]["tools"], "tools[] drifted"
    # Only ever extend the cached prefix: every already-sent message is
    # re-sent byte-identically on the next request.
    for prev, nxt in zip(reqs, reqs[1:]):
        n = len(prev["messages"])
        assert len(nxt["messages"]) > n
        assert _canon(_cache_view(nxt["messages"][:n])) == _canon(
            _cache_view(prev["messages"])
        ), (
            "already-sent messages were rewritten"
        )
