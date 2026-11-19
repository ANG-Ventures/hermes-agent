"""A fallback lane on the SAME claude-pool relay pool must not end the turn (t_5188f4ed).

Measured 2026-11-17 08:03-08:25 PST (Studio logs/agent.log, session 20261106_202807_acceead9):

  (a) claude-alr answered 503 draining-for-deploy for 150 s; the loop logged "relay drain
      outlived the 150s wait -> fallback" and moved to claude-dtlr, which is the same relay
      program over the same 26 seats and was draining too. The drain ended ~08:06:20, about
      187 s after it started.
  (b) 08:06:24 claude-dtlr answered 400 context_length_exceeded far below the believed 1M
      window; the loop compressed once (230 -> 230 messages, a no-op) and failed the turn,
      although claude-alr (the pinned route) was serving again.
  (c) 2026-11-16 22:23: alr -> dtlr fallbacks on "Relay reports no capacity ... exhausted 20
      of 26" where dtlr reported the same exhaustion (same seats).

The stub below is the relay pool: claude-alr / claude-dtlr / claude-btpr are faces of one
claude-pool deploy over one seat set (all four Studio relays report the same
``fable_allowance`` 20/26 and ``tiers.main`` 26 on /health); claude-elr is the separate
API-credit relay. Time in the drain wait runs on a fake clock so a 185 s drain takes no
wall time.
"""

from __future__ import annotations

import time as _real_time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from agent.fallback_wall_budget import RelayModelExhausted
from run_agent import AIAgent

PORTS = {"claude-alr": 18818, "claude-dtlr": 18816, "claude-btpr": 18811, "claude-elr": 18821}
POOL = frozenset({"claude-alr", "claude-dtlr", "claude-btpr"})
PRIMARY = ("claude-alr", "claude-fable-5-2")
# The live lane order (Studio config.yaml fallback_providers, 2026-11-17); never reordered here.
CHAIN = [
    {"provider": "claude-dtlr", "model": "claude-fable-5-2"},
    {"provider": "claude-btpr", "model": "claude-fable-5-2"},
    {"provider": "claude-alr", "model": "claude-opus-5-5"},
    {"provider": "claude-dtlr", "model": "claude-opus-5-5"},
    {"provider": "claude-btpr", "model": "claude-opus-5-5"},
    {"provider": "claude-elr", "model": "claude-opus-5-5"},
]
# Verbatim shape of the 08:06:24 claude-dtlr refusal (claude-bpx tuiRunner context_length_exceeded).
CONTEXT_400 = {"error": {"type": "invalid_request_error", "code": "context_length_exceeded",
                         "message": "prompt is too long: the interactive session hit the model "
                                    "context limit; compact the history"}}


def _base(provider: str) -> str:
    return f"http://127.0.0.1:{PORTS[provider]}/v1"


class FakeClock:
    """``time`` stand-in for agent.turn_recovery: the drain wait sleeps on it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += max(float(s), 0.0)

    def time(self):
        return _real_time.time() + self.now

    def __getattr__(self, name):
        return getattr(_real_time, name)


class StubPool:
    """One claude-pool deploy: every member face drains together and shares the seats."""

    def __init__(self, clock, *, drain_s=0.0, exhausted=(), context_ceiling=None, conn_errors=0):
        self.clock = clock
        self.drain_until = clock.now + drain_s
        self.exhausted = set(exhausted)
        self.context_ceiling = dict(context_ceiling or {})
        self.conn_errors = conn_errors
        self.calls = []

    def call(self, provider, model):
        self.calls.append((provider, model))
        if provider == PRIMARY[0] and self.conn_errors > 0:
            self.conn_errors -= 1
            raise openai.APIConnectionError(request=httpx.Request("POST", _base(provider)))
        if provider in POOL and self.clock.now < self.drain_until:
            body = {"error": "draining-for-deploy", "retry_after": 15}
            req = httpx.Request("POST", f"{_base(provider)}/chat/completions")
            resp = httpx.Response(503, request=req, json=body, headers={"retry-after": "15"})
            raise openai.InternalServerError(f"Error code: 503 - {body}", response=resp,
                                             body=body["error"])
        if provider in POOL and model in self.exhausted:
            raise RelayModelExhausted(
                f"relay has no capacity for {model} (Fable allowance used up on 20/26 seats, "
                "no seat eligible); moving to the next lane",
                capacity={"model": model, "exhausted": 20, "of": 26, "eligible": 0}, lane=provider)
        if (provider, model) in self.context_ceiling:
            req = httpx.Request("POST", f"{_base(provider)}/chat/completions")
            resp = httpx.Response(400, request=req, json=CONTEXT_400)
            raise openai.BadRequestError(f"Error code: 400 - {CONTEXT_400}", response=resp,
                                         body=CONTEXT_400["error"])
        msg = SimpleNamespace(content=f"served by {provider}/{model}", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                               model=model, usage=None)


def _make_agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="k-abcdef123456", base_url=_base(PRIMARY[0]), provider=PRIMARY[0],
            model=PRIMARY[1], quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=[dict(e) for e in CHAIN],
        )
        agent.client = MagicMock()
        agent._api_max_retries = 3
        return agent


def _run(agent, pool, clock):
    fb_client = MagicMock()
    fb_client.api_key = "k-abcdef123456"
    fb_client._custom_headers = None
    fb_client.default_headers = None

    def _resolve(provider, model=None, **kw):
        fb_client.base_url = kw.get("explicit_base_url") or _base(provider)
        return fb_client, model

    compress = MagicMock(side_effect=lambda msgs, sysmsg, **kw: (msgs, sysmsg))
    with (
        patch.object(agent, "_interruptible_api_call",
                     side_effect=lambda kw: pool.call(agent.provider, agent.model)),
        # A route rebuilt mid-turn (restore to the pinned route) may stream: same stub.
        patch.object(agent, "_interruptible_streaming_api_call",
                     side_effect=lambda kw, **_: pool.call(agent.provider, agent.model)),
        patch.object(agent, "_compress_context", compress),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch("agent.turn_recovery.time", clock),
        patch("agent.fallback_wiring.local_relay_restart_wait_s", return_value=0.0),
        patch("agent.retry_utils.jittered_backoff", return_value=0.0),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=1_000_000),
    ):
        result = agent.run_conversation("hello")
    return result, compress


@pytest.fixture(autouse=True)
def _fresh_ceilings():
    try:
        from agent import context_ceilings
    except ImportError:
        yield
        return
    context_ceilings.clear()
    yield
    context_ceilings.clear()


# ── (a) primary drains 185 s while the fallback lane drains too ──────────────

def test_a_pool_drain_185s_is_waited_out_on_the_pinned_route():
    clock = FakeClock()
    pool = StubPool(clock, drain_s=185.0)
    result, _ = _run(_make_agent(), pool, clock)
    assert result["completed"] is True, result.get("error")
    assert result["final_response"] == "served by claude-alr/claude-fable-5-2"
    lanes = {p for p, _ in pool.calls}
    assert lanes == {"claude-alr"}, f"a same-pool lane was tried during a pool drain: {pool.calls}"


def test_a_drain_past_the_wait_skips_every_same_pool_lane():
    clock = FakeClock()
    pool = StubPool(clock, drain_s=10_000.0)
    result, _ = _run(_make_agent(), pool, clock)
    assert result["final_response"] == "served by claude-elr/claude-opus-5-5"
    assert {p for p, _ in pool.calls} == {"claude-alr", "claude-elr"}, pool.calls


# ── (b) fallback lane 400s below the believed window; the primary is healthy again ──

def test_b_context_rejection_on_a_fallback_rung_does_not_end_the_turn():
    clock = FakeClock()
    pool = StubPool(clock, conn_errors=2, context_ceiling={("claude-dtlr", "claude-fable-5-2"): 1})
    result, compress = _run(_make_agent(), pool, clock)
    assert result["completed"] is True, result.get("error") or result.get("final_response")
    assert result["final_response"] == "served by claude-alr/claude-fable-5-2", pool.calls
    assert ("claude-dtlr", "claude-fable-5-2") in pool.calls
    compress.assert_not_called()  # the pinned route never rejected: no compression
    from agent import context_ceilings

    assert context_ceilings.ceiling("claude-dtlr", "claude-fable-5-2") is not None


def test_b_rung_that_rejected_is_not_retried_when_the_pinned_route_is_still_down():
    clock = FakeClock()
    pool = StubPool(clock, conn_errors=99, context_ceiling={("claude-dtlr", "claude-fable-5-2"): 1})
    result, compress = _run(_make_agent(), pool, clock)
    assert result["completed"] is True, result.get("error") or result.get("final_response")
    assert result["final_response"] == "served by claude-btpr/claude-fable-5-2", pool.calls
    assert pool.calls.count(("claude-dtlr", "claude-fable-5-2")) == 1, pool.calls
    compress.assert_not_called()


# ── (c) primary and fallback both report the same model exhausted on the same pool ──

def test_c_pool_exhaustion_skips_same_pool_lanes_for_that_model():
    clock = FakeClock()
    pool = StubPool(clock, exhausted={"claude-fable-5-2"})
    result, _ = _run(_make_agent(), pool, clock)
    assert result["final_response"] == "served by claude-alr/claude-opus-5-5"
    assert pool.calls == [("claude-alr", "claude-fable-5-2"), ("claude-alr", "claude-opus-5-5")], pool.calls


# ── 3. the "too large" message and the ceiling log quote the real count when one is known ──

def _overflow_on_pinned_route(messages, anchor, caplog):
    """The PINNED route rejects (no fallback active): compress once, then the turn ends kept."""
    from agent.turn_overflow import _Recovery, _recover_context_length
    from agent.turn_retry_state import TurnRetryState

    agent = SimpleNamespace(
        model="claude-fable-5-2", log_prefix="", provider="claude-dtlr", base_url=_base("claude-dtlr"),
        tools=None, max_tokens=None, _usage_anchor=anchor, _fallback_activated=False,
        context_compressor=SimpleNamespace(context_length=1_000_000),
        _flush_status_buffer=lambda: None, _persist_session=lambda *a, **k: None,
        _vprint=lambda *a, **k: None, _buffer_vprint=lambda *a, **k: None,
        _buffer_diagnostic_status=lambda *a, **k: None,
    )
    st = _Recovery(agent=agent, api_messages=messages, system_message=None, effective_task_id="t",
                   api_call_count=2, max_compression_attempts=3, messages=messages,
                   active_system_prompt=None, conversation_history=None, approx_tokens=244_603,
                   compression_attempts=1)
    err = f"Error code: 400 - {CONTEXT_400}"
    with caplog.at_level("WARNING", logger="agent.conversation_loop"), \
         patch("agent.model_metadata.estimate_request_tokens_rough", return_value=244_603):
        verdict = _recover_context_length(st, TurnRetryState(), err)
    return verdict


def test_message_quotes_the_provider_count_when_a_usage_anchor_matches(caplog):
    from agent.usage_anchor import capture_usage_anchor

    priced = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    anchor = capture_usage_anchor(346_000, 615, priced)
    verdict = _overflow_on_pinned_route(priced + [{"role": "user", "content": "next"}], anchor, caplog)
    text = verdict.result["final_response"]
    assert "346,6" in text and "counted by the provider" in text, text
    assert "244,603" not in text
    assert "token_basis=measured" in caplog.text


def test_message_says_estimate_without_a_provider_count(caplog):
    verdict = _overflow_on_pinned_route([{"role": "user", "content": "q"}], None, caplog)
    text = verdict.result["final_response"]
    assert "244,603 tokens (estimate)" in text, text
    assert "token_basis=estimate" in caplog.text


# ── send-back round 2: pool-wide evidence only, endpoint-keyed ceilings, no stale size ──

@pytest.mark.parametrize("headers,text,noted", [
    ({"x-relay-error-class": "quota_model"}, "no eligible sub for the requested model", True),
    # "no eligible sub" classed from text alone can be a lane fact (bridge mode, pool exclusion).
    ({}, "no eligible sub", False),
    ({"x-relay-error-class": "pool_pressure"}, "no eligible sub", False),
])
def test_pool_skip_keys_on_relay_stated_quota_only(headers, text, noted):
    from agent.chat_completion_helpers import _note_pool_exhausted_model

    agent = SimpleNamespace(provider="claude-dtlr", model="claude-opus-5-5", _pool_exhausted_models=None,
                            _pending_fallback_error={"at": _real_time.monotonic(), "text": text,
                                                     "status": 503, "headers": headers})
    _note_pool_exhausted_model(agent)
    assert ("claude-opus-5-5" in (agent._pool_exhausted_models or ())) is noted


def test_ceiling_is_keyed_on_the_declared_endpoint():
    from agent import context_ceilings as cc

    cc.record("claude-dtlr", "claude-fable-5-2", "http://127.0.0.1:18816/v1/", 100_000)
    assert cc.too_large_for("claude-dtlr", "claude-fable-5-2", "http://127.0.0.1:18816/v1", 150_000)
    assert not cc.too_large_for("claude-dtlr", "claude-fable-5-2", None, 150_000)
    assert not cc.too_large_for("claude-dtlr", "claude-fable-5-2", "http://other:1/v1", 150_000)


@pytest.mark.parametrize("request_size,viable", [(150_000, False), (50_000, True)])
def test_wall_budget_viability_honours_a_recorded_ceiling(request_size, viable):
    from agent import context_ceilings as cc
    from agent.fallback_wall_budget import _lane_ahead

    cc.record("claude-dtlr", "claude-fable-5-2", None, 100_000)
    agent = SimpleNamespace(_fallback_chain=[{"provider": "claude-dtlr", "model": "claude-fable-5-2"}],
                            _fallback_index=0,
                            context_compressor=SimpleNamespace(last_prompt_tokens=request_size))
    assert _lane_ahead(agent) is viable


def test_compression_clears_the_rejected_request_size():
    from agent.context_ceilings import request_tokens

    agent = _make_agent()
    agent._context_rejected_tokens = 300_000
    agent.context_compressor.last_prompt_tokens = 0
    msgs = [{"role": "user", "content": "x"}]
    with patch("agent.conversation_compression.compress_context",
               side_effect=lambda self, m, s, **kw: (list(m[:1]), s)), \
         patch("agent.conversation_compression.resolve_context_compression_timeouts", return_value=(0, 0)):
        agent._compress_context(msgs, "sys")
    assert request_tokens(agent) == 0
    # A no-op pass (same list back) keeps it: the request did not change.
    agent._context_rejected_tokens = 300_000
    with patch("agent.conversation_compression.compress_context",
               side_effect=lambda self, m, s, **kw: (m, s)), \
         patch("agent.conversation_compression.resolve_context_compression_timeouts", return_value=(0, 0)):
        agent._compress_context(msgs, "sys")
    assert request_tokens(agent) == 300_000
