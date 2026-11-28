"""Streaming 5xx unmasking: the non-streaming probe surfaces the provider's real error.

Regression shape (AssemblyAI LLM Gateway, 2026-09): a gateway that validates requests
only on its non-streaming path returns an opaque ``500 something went wrong`` when
streaming the SAME request. _handle_stream_error must re-issue once non-streaming on a
pre-delta 5xx so the actionable 4xx (or a successful response) reaches the user instead
of three identical opaque 500s.

Gating invariants (from cross-vendor review):
- never after partial delivery, on non-5xx, or without an HTTP status
- one probe per 60s window on the AGENT (outer retries build fresh _StreamingCall
  instances, so an instance flag would re-probe every attempt)
- chat-completions wire only (adoption replays chat-completions shapes)
- probe success delivers for the turn WITHOUT latching _disable_streaming
  (a transient gateway 500 must not permanently disable streaming)
- the recovered delivery opens and closes its own stream pair (consumers must never
  see deltas after the failed attempt's terminal on_stream_end)
- user interrupts re-raise; never swallowed
"""
from types import SimpleNamespace

from agent import chat_completion_helpers as h


def _make_call(api_kwargs, *, deltas_sent=False, api_mode="chat_completions"):
    call = h._StreamingCall.__new__(h._StreamingCall)
    call.agent = SimpleNamespace(
        provider="custom", model="gpt-5.6-sol", api_mode=api_mode,
        _interrupt_requested=False,
        # Fork contract: the stream-parse classifier also receives http_status=.
        _is_provider_stream_parse_error=lambda e, **_kw: False,
        _buffer_status=lambda text: call.buffered.append(text),
        _disable_streaming=False, _stream_5xx_probe_ts=None,
        _fire_stream_delta=lambda text: call.deltas.append(text),
    )
    call.buffered = []
    call.deltas = []
    call.first_delta_fired = {"done": True}
    call.on_first_delta = None
    call._stream_stale_timeout = 180.0
    call._dispatch = None
    call.api_kwargs = api_kwargs
    call.result = {"response": None, "error": None, "partial_tool_names": []}
    call.deltas_were_sent = {"yes": deltas_sent}
    call.provider_tool_in_flight = {"yes": False}
    call._request_cancelled = {"value": False}
    # Fork: _handle_stream_error reads the stream diag (http_status) off the client pair.
    call.clients = SimpleNamespace(diag={}, close_once=lambda *_a, **_k: None)
    return call


class _StreamErr(Exception):
    """Stands in for the openai 5xx the stream path raises."""

    def __init__(self, status):
        super().__init__(f"Error code: {status} - something went wrong")
        self.status_code = status


class _ProbeErr(Exception):
    def __init__(self, status):
        super().__init__(f"Error code: {status} - real validation message")
        self.status_code = status


def _run_handle_stream_error(call, e):
    return call._handle_stream_error(e, attempt=2, max_retries=2)


def test_stream_5xx_probe_success_delivers_response_without_latch(monkeypatch):
    call = _make_call({"model": "m", "stream": True, "stream_options": {"x": 1}, "messages": []})
    seen_kwargs = []

    def fake_probe(agent, kwargs, **_kw):
        seen_kwargs.append((kwargs, call._stream_stale_timeout))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning_content=None))])

    monkeypatch.setattr(h, "interruptible_api_call", fake_probe)

    handled = not _run_handle_stream_error(call, _StreamErr(500))

    assert handled
    assert call.result["response"] is not None
    assert call.result["error"] is None
    probe_kwargs, stale_during_probe = seen_kwargs[0]
    assert "stream" not in probe_kwargs and "stream_options" not in probe_kwargs
    assert call.deltas == ["ok"]
    # One-turn recovery: streaming is never latched off for the session.
    assert call.agent._disable_streaming is False
    # No chunks arrive during the probe: the stream monitor must not stale-kill it.
    assert stale_during_probe == float("inf") and call._stream_stale_timeout == 180.0
    assert call.buffered and "non-streaming retry succeeded" in call.buffered[0]


def test_stream_5xx_unmasked_by_probe_4xx(monkeypatch):
    call = _make_call({"model": "m", "messages": []})
    real = _ProbeErr(400)

    def fake_probe(agent, kwargs, **_kw):
        raise real

    monkeypatch.setattr(h, "interruptible_api_call", fake_probe)

    handled = _run_handle_stream_error(call, _StreamErr(500))

    assert not handled  # loop stops, error propagates
    assert call.result["error"] is real  # the REAL validation error replaces the opaque 500
    assert call.result["response"] is None


def test_stream_5xx_probe_is_ledgered_on_the_stream_dispatch(monkeypatch):
    """t_beeca1dd: the probe can run on an orphaned stream thread after the turn
    failed over (claude-btpr -> cpa); it must be recorded on the stream's own
    dispatch, never on the agent's live route at probe time."""
    call = _make_call({"model": "m", "messages": []})
    call._dispatch = {"provider": "claude-btpr", "model": "claude-opus-5-5"}
    call.agent._inflight_request_route = {"provider": "cpa", "model": "gpt-6.1-sol"}
    seen = []

    def fake_probe(agent, kwargs, *, dispatch=None):
        seen.append(dispatch)
        raise _ProbeErr(502)

    monkeypatch.setattr(h, "interruptible_api_call", fake_probe)

    # An opaque 500 (a 503 is a capacity answer and is never re-sent, t_17b7602b).
    _run_handle_stream_error(call, _StreamErr(500))

    assert seen == [call._dispatch]


# --- t_17b7602b: relay / capacity 5xx have nothing to unmask -------------------------------------
# On relay lanes a pre-delta 5xx is the stack saying "no capacity here" (with Retry-After), not an
# opaque gateway crash. Studio agent.log 2026-11-27: 117 re-sends, 0 surfaced a 4xx; one hidden
# re-send held a turn 120 s before the loop heard of the 503. These go straight to the loop.

import httpx
import openai
import pytest


def _sdk_status_error(status, body, headers=None):
    """The exact exception the openai SDK raises for this HTTP answer."""
    client = openai.OpenAI(api_key="x", base_url="http://door.invalid/v1")
    resp = httpx.Response(status, headers=headers or {}, json=body,
                          request=httpx.Request("POST", "http://door.invalid/v1/chat/completions"))
    return client._make_status_error_from_response(resp)


_LANE_DOOR_503 = (
    503,
    {"error": "local_unavailable", "relay_error_class": "pool_pressure",
     "relay_error_hop": "front", "relay_error_cause": "local_unavailable"},
    {"x-relay-error-class": "pool_pressure", "x-relay-error-hop": "front",
     "x-relay-error-cause": "local_unavailable", "retry-after": "1"},
)
_DRAIN_503 = (503, {"error": "draining-for-deploy"}, {"retry-after": "5"})
_RELAY_504 = (504, {"error": "upstream attempt timed out"}, {})


@pytest.mark.parametrize("status,body,headers", [_LANE_DOOR_503, _DRAIN_503, _RELAY_504],
                         ids=["lane-door-503-local_unavailable", "503-draining-for-deploy",
                              "504-upstream-attempt-timed-out"])
def test_relay_or_capacity_5xx_goes_to_loop_without_resend(monkeypatch, status, body, headers):
    call = _make_call({"model": "m", "stream": True, "messages": []})
    sent = []
    monkeypatch.setattr(h, "interruptible_api_call", lambda *a, **k: sent.append(a) or None)
    err = _sdk_status_error(status, body, headers)

    retry_stream = _run_handle_stream_error(call, err)

    assert sent == []  # no second request
    assert retry_stream is False
    assert call.result["error"] is err and call.result["response"] is None
    assert call.agent._stream_5xx_probe_ts is None  # the 60 s probe window is not spent


@pytest.mark.parametrize("headers,body", [
    ({"x-relay-error-hop": "relay->bridge"}, {"error": {"message": "boom"}}),
    ({}, {"error": {"message": "boom", "relay_error_class": "conn"}}),
], ids=["hop-header", "class-in-body"])
def test_plain_500_with_relay_fields_is_not_resent(monkeypatch, headers, body):
    call = _make_call({"model": "m", "messages": []})
    sent = []
    monkeypatch.setattr(h, "interruptible_api_call", lambda *a, **k: sent.append(a) or None)
    err = _sdk_status_error(500, body, headers)

    assert _run_handle_stream_error(call, err) is False
    assert sent == [] and call.result["error"] is err


def test_opaque_sdk_500_still_gets_the_one_resend(monkeypatch):
    call = _make_call({"model": "m", "stream": True, "messages": []})
    sent = []

    def fake_probe(agent, kwargs, **_kw):
        sent.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning_content=None))])

    monkeypatch.setattr(h, "interruptible_api_call", fake_probe)
    err = _sdk_status_error(500, {"error": {"message": "something went wrong"}})

    assert _run_handle_stream_error(call, err) is False
    assert len(sent) == 1 and call.result["response"] is not None and call.result["error"] is None


# --- t_41183e3a: untagged relay stalls; relay field names only as real keys ----------------------

def _resend_count(monkeypatch, err):
    call = _make_call({"model": "m", "stream": True, "messages": []})
    sent = []

    def fake_probe(agent, kwargs, **_kw):
        sent.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning_content=None))])

    monkeypatch.setattr(h, "interruptible_api_call", fake_probe)
    assert _run_handle_stream_error(call, err) is False
    return len(sent), call


@pytest.mark.parametrize("body", [
    {"error": "pool deadline exceeded"},
    {"error": {"message": "interactive turn timed out", "code": "tui_turn_timeout"}},
], ids=["pool-deadline-exceeded", "tui_turn_timeout"])
def test_untagged_relay_stall_504_goes_to_loop_without_resend(monkeypatch, body):
    err = _sdk_status_error(504, body)
    n, call = _resend_count(monkeypatch, err)
    assert n == 0 and call.result["error"] is err and call.result["response"] is None


def test_unrelated_504_still_gets_the_one_resend(monkeypatch):
    n, call = _resend_count(monkeypatch, _sdk_status_error(504, {"error": {"message": "gateway timeout"}}))
    assert n == 1 and call.result["response"] is not None


def test_relay_field_name_only_inside_a_value_still_gets_the_one_resend(monkeypatch):
    body = {"error": {"message": "something went wrong", "request_id": "relay_error_hop-42"}}
    n, call = _resend_count(monkeypatch, _sdk_status_error(500, body))
    assert n == 1 and call.result["response"] is not None


@pytest.mark.parametrize("status,body", [
    (503, {"error": "local_unavailable", "relay_error_class": "pool_pressure", "relay_error_hop": "front"}),
    (503, {"error": {"message": "local_unavailable"}, "relay_error_class": "pool_pressure",
           "relay_error_hop": "front"}),
    (500, {"error": {"message": "local_unavailable"}, "relay_error_class": "pool_pressure",
           "relay_error_hop": "front"}),
    (502, {"error": "bad gateway", "relay_error_hop": "relay->bridge"}),
], ids=["503-flat", "503-nested-top-level-fields", "500-nested-top-level-fields", "502-flat-hop"])
def test_relay_fields_as_top_level_keys_without_headers_skip_resend(monkeypatch, status, body):
    err = _sdk_status_error(status, body)
    n, call = _resend_count(monkeypatch, err)
    assert n == 0 and call.result["error"] is err
