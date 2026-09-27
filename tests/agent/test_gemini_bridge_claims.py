"""gemini-bridge attribution claims on auxiliary calls (SPEC §3.B, Phase 2).

The bridge records x-hermes-profile / x-hermes-aux-task / x-hermes-session as
claimed fields. These tests pin who sends them (gemini-bridge only), what they
carry, and that a caller's own extra_headers no longer wipe them.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent import aux_accounting
from agent.auxiliary_client import _build_call_kwargs, call_llm
from agent.fork_ext import gemini_bridge_claims as claims


@pytest.fixture(autouse=True)
def _profile_and_session(monkeypatch):
    monkeypatch.setattr(
        "agent.fork_ext.relay_headers._call_id_profile", lambda: "athena"
    )
    token = aux_accounting.set_accounting_context(object(), "20260927_120000_abc123")
    yield
    aux_accounting.reset_accounting_context(token)


def _kwargs(provider, task="title_generation"):
    return _build_call_kwargs(
        provider, "m", [{"role": "user", "content": "hi"}], task=task
    )


@pytest.mark.parametrize("provider", ["gemini-bridge", "Gemini-Bridge", "gemini-ultra", "antigravity"])
def test_bridge_calls_carry_all_three_claims(provider):
    assert _kwargs(provider)["extra_headers"] == {
        "x-hermes-profile": "athena",
        "x-hermes-aux-task": "title_generation",
        "x-hermes-session": "20260927_120000_abc123",
    }


@pytest.mark.parametrize("provider", ["claude-bpr", "openrouter", "custom", "gemini", "", None])
def test_other_providers_never_get_claims(provider):
    assert "extra_headers" not in _kwargs(provider)


def test_values_use_the_bridge_claim_charset_and_limit():
    aux_accounting.set_accounting_context(object(), "sess id\r\nx-evil: 1" + "z" * 100)
    out = claims.claim_headers("gemini-bridge", "vision task")
    assert out["x-hermes-aux-task"] == "vision_task"
    sess = out["x-hermes-session"]
    assert len(sess) == 64 and "\r" not in sess and "\n" not in sess and " " not in sess


def test_missing_task_and_session_degrade_without_raising():
    aux_accounting.set_accounting_context(None, None)
    assert claims.claim_headers("gemini-bridge", None) == {
        "x-hermes-profile": "athena",
        "x-hermes-aux-task": "unspecified",
    }


def test_profile_lookup_failure_never_breaks_the_call(monkeypatch):
    def boom():
        raise RuntimeError("no profile")

    monkeypatch.setattr("agent.fork_ext.relay_headers._call_id_profile", boom)
    out = claims.claim_headers("gemini-bridge", "vision")
    assert "x-hermes-profile" not in out and out["x-hermes-aux-task"] == "vision"


def test_caller_extra_headers_merge_over_claims():
    kw = _kwargs("gemini-bridge")
    claims.merge_extra_headers(kw, {"x-initiator": "user", "x-hermes-aux-task": "override"})
    assert kw["extra_headers"]["x-initiator"] == "user"
    assert kw["extra_headers"]["x-hermes-aux-task"] == "override"
    assert kw["extra_headers"]["x-hermes-profile"] == "athena"


class _FakeBridge(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps({
            "id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "A Title"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_bridge(monkeypatch):
    """Loopback bridge; the aux client for ``gemini-bridge`` is a real OpenAI
    SDK client pointed at it.

    The provider profile ships as a user plugin the hermetic test home lacks,
    so only client construction is substituted. Provider/task resolution,
    kwargs building, header merging and the SDK HTTP send all run for real.
    """
    import agent.auxiliary_client as aux

    _FakeBridge.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeBridge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    built = []

    def _client(provider, model, **_kw):
        built.append(provider)
        return aux._create_openai_client(api_key="test-key", base_url=url), model

    monkeypatch.setattr(aux, "_get_cached_client", _client)
    try:
        yield built
    finally:
        srv.shutdown()


def test_claims_reach_the_wire_through_call_llm(fake_bridge):
    """Real call_llm -> OpenAI SDK -> HTTP; caller headers must not erase claims."""
    resp = call_llm(
        task="title_generation",
        provider="gemini-bridge",
        model="gemini-3.8-flash-medium",
        messages=[{"role": "user", "content": "hello"}],
        extra_headers={"x-caller": "1"},
    )
    assert fake_bridge == ["gemini-bridge"]
    assert resp.choices[0].message.content == "A Title"
    (hdrs,) = _FakeBridge.seen
    assert hdrs["x-hermes-aux-task"] == "title_generation"
    assert hdrs["x-hermes-profile"] == "athena"
    assert hdrs["x-hermes-session"] == "20260927_120000_abc123"
    assert hdrs["x-caller"] == "1"
