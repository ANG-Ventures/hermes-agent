"""Every auxiliary caller of a claude-pool relay waits out a deploy-drain 503.

Wire shape (claude-pool claude_pool_relay.py, t_826861ab + v2 stamp t_ef5f9f1e):
    503  Retry-After: 15
         x-relay-error-class: pool_pressure  x-relay-error-hop: relay
         x-relay-error-cause: deploy-drain
    {"error": "draining-for-deploy", "retry_after": 15,
     "relay_error_class": "pool_pressure", "relay_error_hop": "relay",
     "relay_error_cause": "deploy-drain"}

Before (fork/main 28eecce0e0): the auxiliary client treated it as a generic
transient 5xx (1 s + 2 s backoff, Retry-After ignored) and raised to the caller.
The LCM summariser then recorded a route failure per escalation level, opened
its summary circuit and committed a level-3 deterministic truncation.

Served by a real local HTTP relay stub through the real OpenAI SDK client, so
the SDK's body handling (``body = data["error"]``) is the production one.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest

from agent import auxiliary_client as aux
from agent.error_classifier import is_relay_deploy_drain
from plugins.context_engine.lcm import escalation

DRAIN_BODY = {
    "error": "draining-for-deploy", "retry_after": 15,
    "relay_error_class": "pool_pressure", "relay_error_hop": "relay",
    "relay_error_cause": "deploy-drain",
}
DRAIN_HEADERS = {
    "Retry-After": "15", "x-relay-error-class": "pool_pressure",
    "x-relay-error-hop": "relay", "x-relay-error-cause": "deploy-drain",
    "x-relay-seat": "none", "x-relay-seats-tried": "0", "x-relay-eligible": "?",
}
SUMMARY = "Decisions retained: ship the drain wait."


class _Relay:
    """Answers the first ``drains`` requests with the drain 503, then serves."""

    def __init__(self, drains: int, body: dict | None = None, status: int = 503):
        self.drains = drains
        self.body = body or DRAIN_BODY
        self.status = status
        self.requests = 0
        relay = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                relay.requests += 1
                if relay.requests <= relay.drains:
                    data = json.dumps(relay.body).encode()
                    self.send_response(relay.status)
                    for k, v in DRAIN_HEADERS.items():
                        self.send_header(k, v)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if req.get("stream"):
                    chunks = [
                        {"id": "c1", "object": "chat.completion.chunk", "created": 0,
                         "model": req.get("model"), "choices": [
                             {"index": 0, "delta": {"role": "assistant", "content": SUMMARY},
                              "finish_reason": None}]},
                        {"id": "c1", "object": "chat.completion.chunk", "created": 0,
                         "model": req.get("model"), "choices": [
                             {"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}},
                    ]
                    data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                    data = data.encode()
                    ctype = "text/event-stream"
                else:
                    data = json.dumps({
                        "id": "c1", "object": "chat.completion", "created": 0,
                        "model": req.get("model"),
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": SUMMARY}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
                    }).encode()
                    ctype = "application/json"
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def relay_route(monkeypatch):
    """Route the ``compression`` aux task to a local relay stub; record drain waits."""
    made: list[_Relay] = []
    waits: list[float] = []

    def make(drains: int, *, wait_budget_s: float = 150.0, async_mode: bool = False, **kw):
        relay = _Relay(drains, **kw)
        made.append(relay)
        cls = openai.AsyncOpenAI if async_mode else openai.OpenAI
        client = cls(api_key="test", base_url=relay.base_url, max_retries=0)
        monkeypatch.setattr(aux, "_resolve_task_provider_model",
                            lambda *a, **k: ("claude-bpr", "claude-sonnet-5-5", None, None, None))
        monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, "claude-sonnet-5-5"))
        monkeypatch.setattr(aux, "_get_auxiliary_task_config",
                            lambda task: {"provider": "claude-bpr", "model": "claude-sonnet-5-5"})
        monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0)
        monkeypatch.setattr(aux, "_transient_retry_count", lambda: 2)
        monkeypatch.setattr(aux, "_task_minimum_context_length", lambda task: None, raising=False)
        monkeypatch.setattr(aux, "_record_aux_call_cost", lambda *a, **k: None, raising=False)
        monkeypatch.setattr("agent.aux_accounting.record_aux_api_call", lambda *a, **k: None)
        monkeypatch.setattr("agent.fallback_wiring.relay_drain_wait_s", lambda: wait_budget_s)
        # Record the wait instead of sleeping 15 s of wall clock per drain reply.
        monkeypatch.setattr(aux, "_relay_drain_sleep", lambda s: waits.append(s), raising=False)

        async def _arecord(s):
            waits.append(s)
        monkeypatch.setattr(aux, "_arelay_drain_sleep", _arecord, raising=False)
        return relay

    yield make, waits
    for r in made:
        r.close()


def _content(resp) -> str:
    return resp.choices[0].message.content


# ── classifier: the public check sees the wire shape, nothing else ─────────

def test_public_drain_check_matches_the_v2_wire_and_nothing_else(relay_route):
    make, _ = relay_route
    relay = make(drains=1)
    client = openai.OpenAI(api_key="t", base_url=relay.base_url, max_retries=0)
    with pytest.raises(openai.APIStatusError) as ei:
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])
    assert is_relay_deploy_drain(ei.value)

    other = make(drains=1, body={"error": "no eligible sub"})
    client = openai.OpenAI(api_key="t", base_url=other.base_url, max_retries=0)
    with pytest.raises(openai.APIStatusError) as ei:
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])
    assert not is_relay_deploy_drain(ei.value)


# ── caller class: auxiliary client, sync (memory/title/compression/kanban) ──

def test_sync_call_llm_waits_out_the_drain_on_the_same_model(relay_route):
    make, waits = relay_route
    relay = make(drains=6)  # outlasts the 1 s + 2 s transient retries
    resp = aux.call_llm(task="compression", messages=[{"role": "user", "content": "hi"}],
                        max_tokens=50)
    assert _content(resp) == SUMMARY
    assert relay.requests == 7
    assert waits == [15.0] * 6  # Retry-After honoured


def test_sync_call_llm_gives_up_after_the_drain_budget(relay_route):
    make, waits = relay_route
    relay = make(drains=99, wait_budget_s=0.0)  # 0 disables the wait
    with pytest.raises(openai.APIStatusError) as ei:
        aux.call_llm(task="compression", messages=[{"role": "user", "content": "hi"}],
                     max_tokens=50)
    assert is_relay_deploy_drain(ei.value)
    # No 1 s/2 s transient retries on a drain the budget already gave up on.
    assert relay.requests == 1
    assert waits == []


# ── caller class: auxiliary client, async (session search, async aux tasks) ─

def test_async_call_llm_waits_out_the_drain_on_the_same_model(relay_route):
    make, waits = relay_route
    relay = make(drains=6, async_mode=True)
    resp = asyncio.run(aux.async_call_llm(
        task="compression", messages=[{"role": "user", "content": "hi"}], max_tokens=50))
    assert _content(resp) == SUMMARY
    assert relay.requests == 7
    assert waits == [15.0] * 6  # Retry-After honoured


def test_drain_sleep_is_interruptible(monkeypatch):
    """A host cancel aborts the drain wait (BaseException, not swallowed)."""
    monkeypatch.setattr(aux, "_aux_interrupt_cancel_requested", lambda: True)
    with pytest.raises(aux.AuxiliaryExplicitCancellation):
        aux._relay_drain_sleep(15.0)
    with pytest.raises(aux.AuxiliaryExplicitCancellation):
        asyncio.run(aux._arelay_drain_sleep(15.0))


# ── caller class: LCM leaf/condense summariser ─────────────────────────────

def test_lcm_summary_survives_a_drain_at_level_one_without_tripping_the_circuit(relay_route):
    make, waits = relay_route
    relay = make(drains=6)
    breaker = escalation.SummaryCircuitBreaker()
    text = "conversation detail and decisions. " * 400
    summary, level = escalation.summarize_with_escalation(
        text, source_tokens=4000, token_budget=500, circuit_breaker=breaker)
    assert (summary, level) == (SUMMARY, 1)
    assert breaker._failures == {} and breaker._open_until == {}
    assert relay.requests == 7


def test_lcm_drain_past_the_budget_never_counts_or_truncates(relay_route):
    make, _ = relay_route
    relay = make(drains=10_000, wait_budget_s=0.0)
    breaker = escalation.SummaryCircuitBreaker()
    text = "conversation detail and decisions. " * 400
    with pytest.raises(escalation.SummaryRelayDrainingError):
        escalation.summarize_with_escalation(
            text, source_tokens=4000, token_budget=500, circuit_breaker=breaker)
    assert breaker._failures == {} and breaker._open_until == {}
    assert breaker.allows("")
    # L1 only: no L2 on the same draining relay, no L3 truncation committed.
    assert relay.requests == 1
