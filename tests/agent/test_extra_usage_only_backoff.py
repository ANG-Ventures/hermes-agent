"""t_7f2ced0d: Anthropic's "Third-party apps now draw from your extra usage"
400 is a deterministic refusal for the route that got it. A primary return that fails again with the
same error within seconds must back off (doubling, bounded) and say so in the
announce, instead of recovering into the same 400 every turn.

Fixture is the wire body seen on claude-apr 2026-09-30 14:19:43 (session
20260929_140138_45a33cbb, seat sub-vps-15, claude-fable-5-1).
"""

from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent import fallback_wiring as fw
from agent.agent_runtime_helpers import extract_api_error_context
from agent.error_classifier import FailoverReason, classify_api_error

APR = "http://127.0.0.1:18810/anthropic"
MSG = ("Third-party apps now draw from your extra usage, not your plan limits. "
       "Add more at claude.ai/settings/usage and keep going.")
UTC = dt.timezone.utc


def _bad_request(message: str, request_id: str = "req_011CfaKfE3y42mXv4DPeRm7v",
                 served_by: str = "sub-vps-15") -> anthropic.BadRequestError:
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message},
            "request_id": request_id}
    req = httpx.Request("POST", f"{APR}/v1/messages")
    resp = httpx.Response(400, json=body, request=req, headers={
        "x-pool-served-by": served_by, "x-pool-route-id": "6027d8d924b04f9c864f73e06387a932"})
    return anthropic.BadRequestError(message=f"Error code: 400 - {body}", response=resp, body=body)


def _ts(hms: str) -> float:
    h, m, s = (int(x) for x in hms.split(":"))
    return dt.datetime(2026, 9, 30, h, m, s, tzinfo=UTC).timestamp()


# ── classifier ────────────────────────────────────────────────────────────

def test_third_party_400_is_route_permanent():
    c = classify_api_error(_bad_request(MSG), provider="claude-apr", model="claude-fable-5-1")
    assert c.reason is FailoverReason.extra_usage_only
    assert (c.retryable, c.should_fallback, c.should_rotate_credential) == (False, True, False)
    assert not c.is_auth  # no "authentication failed" guidance for a valid credential


@pytest.mark.parametrize("message,expected", [
    ("max_tokens: 99999 > 64000, which is the maximum allowed number of output tokens "
     "for claude-fable-5-1", FailoverReason.context_overflow),
    ("messages.0.content: Input should be a valid list", FailoverReason.format_error),
    ("You're out of extra usage. Add more at claude.ai/settings/usage", FailoverReason.billing),
])
def test_other_400s_keep_their_class(message, expected):
    c = classify_api_error(_bad_request(message), provider="claude-apr", model="claude-fable-5-1")
    assert c.reason is expected


def test_ledger_class_is_auth_with_an_honest_cause():
    assert fbe.classify_text(MSG, http_status=400) == "auth"
    assert fp._cause_phrase({"trigger_class": "auth", "err_head": MSG}) == fp.THIRD_PARTY_CAUSE
    # the existing auth rows keep their phrase
    assert fp._cause_phrase({"trigger_class": "auth", "err_head": "unauthorized"}) == "OAuth revoked (401)"
    assert "auth" not in fp.STICKY_CLASSES


# ── pure backoff ──────────────────────────────────────────────────────────

def test_backoff_doubles_on_each_same_error_return_and_is_bounded():
    ep = fp.same_error_backoff(None, err_hash="h", now=_ts("14:01:40"), last_return_ts=_ts("14:01:38"),
                               seat="sub-vps-15")
    assert ep["backoff_s"] is None  # first failure: nothing to compare against
    windows = []
    t = _ts("14:01:40")
    for _ in range(6):
        ret = t + 600.0
        ep = fp.same_error_backoff(ep, err_hash="h", now=ret + 2.0, last_return_ts=ret, seat="sub-vps-15")
        windows.append(ep["backoff_s"])
        t = ret + 2.0
    assert windows == [20 * 60, 40 * 60, 80 * 60, 160 * 60, 4 * 3600, 4 * 3600]
    assert fp.SAME_ERR_BACKOFF_CEILING_S == 4 * 3600
    assert fp.SAME_ERR_RETURN_WINDOW_S == 60.0


@pytest.mark.parametrize("kw", [
    dict(err_hash="other"),                         # different error
    dict(now_offset=61.0),                          # failed > SAME_ERR_RETURN_WINDOW_S after the return
    dict(no_return=True),                           # no return since the last failover
])
def test_backoff_not_armed_when_not_a_same_error_return(kw):
    prev = fp.same_error_backoff(None, err_hash="h", now=_ts("14:01:40"), last_return_ts=None)
    ret = _ts("14:19:41")
    ep = fp.same_error_backoff(
        prev, err_hash=kw.get("err_hash", "h"), now=ret + kw.get("now_offset", 2.0),
        last_return_ts=None if kw.get("no_return") else ret)
    assert ep["backoff_s"] is None and ep["repeats"] == 0


# ── end to end: wire error -> wiring -> restore gate -> ledger row -> announce ──

def _agent():
    emitted = []
    return SimpleNamespace(
        provider="claude-apr", model="claude-fable-5-1", session_id="20260929_140138_45a33cbb",
        _current_turn_id="20260929_140138_45a33cbb:7", _primary_runtime={
            "provider": "claude-apr", "model": "claude-fable-5-1", "base_url": APR},
        _fallback_activated=False, _pending_quota_window=None, _pending_pool_scope=None,
        _pending_stream_error_reason=None, _emit_status=emitted.append, emitted=emitted,
    )


def _fail(agent, now):
    err = _bad_request(MSG)
    fbe.stash_api_error(agent, err, 400, extract_api_error_context(err))
    extra = fw.same_error_on_failover(agent, failing=("claude-apr", "claude-fable-5-1"), now=now)
    row = fbe.build_row(agent, "failover", from_provider="claude-apr", from_model="claude-fable-5-1",
                        to_provider="claude-bpr", to_model="claude-opus-5-5",
                        reason=FailoverReason.extra_usage_only, extra=extra)
    row["ts"] = now
    return row


def _announce(agent, row):
    from agent.chat_completion_helpers import _emit_fallback_announce

    agent.emitted.clear()
    agent._last_fallback_announced = None  # the recovery leg between failovers resets the I5 dedupe
    _emit_fallback_announce(agent, "claude-fable-5-1", "claude-opus-5-5", "claude-bpr",
                            old_provider="claude-apr", record_event=False,
                            reason=FailoverReason.extra_usage_only, ledger_row=row)
    return agent.emitted[0] if agent.emitted else ""


def test_same_error_return_says_so_through_the_shipping_emitter(monkeypatch):
    monkeypatch.setattr(fw, "sticky_policy_enabled", lambda: False)
    monkeypatch.setattr(fp, "_hms", lambda ts, tz: dt.datetime.fromtimestamp(float(ts), UTC))
    agent = _agent()

    first = _fail(agent, _ts("14:01:40"))
    assert first["seat"] == "sub-vps-15"          # x-pool-served-by on the 400 passthrough
    assert first["trigger_class"] == "auth"
    assert "same_err_backoff_s" not in first
    assert not (getattr(agent, "_rate_limited_until", 0) or 0)

    fw.note_primary_return(agent, now=_ts("14:19:41"))   # turn-boundary return
    second = _fail(agent, _ts("14:19:43"))               # identical 400, 2 s later
    assert second["err_hash"] == first["err_hash"]       # request_id differs, hash does not
    assert second["same_err_backoff_s"] == 20 * 60

    line = _announce(agent, second)
    assert "plan billing refused (extra usage only)" in line
    assert "same error as 14:01:40 on sub-vps-15; backing off to 20m" in line
    print("\nANNOUNCE_AFTER:", line)

    fw.note_primary_return(agent, now=_ts("14:39:45"))
    third = _fail(agent, _ts("14:39:47"))
    assert third["same_err_backoff_s"] == 40 * 60
    assert "same error as 14:19:43 on sub-vps-15; backing off to 40m" in _announce(agent, third)


# ── real hot path: try_activate_fallback -> restore_primary_runtime -> again ──

from tests.agent.test_fallback_policy import (  # noqa: E402
    _activate, _restore, _rows, _wired_agent, wired,  # noqa: F401  (wired is a fixture)
)


def _stash(agent, rid):
    err = _bad_request(MSG, request_id=rid)
    fbe.stash_api_error(agent, err, 400, extract_api_error_context(err))


def test_hot_path_return_into_same_400_benches_primary(wired):
    home, _ = wired
    a = _wired_agent(provider="claude-apr", model="claude-fable-5-1")
    a._fallback_chain = [{"provider": "claude-bpr", "model": "claude-opus-5-5"}]

    _stash(a, "req_011CfaJHM8PfQreohq3QtbRs")
    assert _activate(a, reason=FailoverReason.extra_usage_only) is True
    assert (a.provider, a.model) == ("claude-bpr", "claude-opus-5-5")

    assert _restore(a) is True                     # next turn: back to apr/fable
    _stash(a, "req_011CfaKfE3y42mXv4DPeRm7v")      # identical 400 seconds later
    assert _activate(a, reason=FailoverReason.extra_usage_only) is True

    f1, f2 = _rows(home, "failover")[-2:]
    assert f1["err_hash"] == f2["err_hash"] and f1["seat"] == f2["seat"] == "sub-vps-15"
    assert f2["trigger_class"] == "auth"
    assert 19 * 60 < f2["cooldown_s"] <= 20 * 60
    assert "same error as " in f2["notice_text"] and "on sub-vps-15; backing off to 20m" in f2["notice_text"]
    print("\nHOT_PATH_NOTICE:", f2["notice_text"])

    assert _restore(a) is False                    # the next turn stays on opus: no cold cycle
    assert (a.provider, a.model) == ("claude-bpr", "claude-opus-5-5")


# ── Prism round 1 (#1581): episode scoping ──────────────────────────────────

APR_FABLE = ("claude-apr", "claude-fable-5-1")


def test_same_hash_on_a_different_route_is_not_a_repeat():
    prev = fp.same_error_backoff(None, err_hash="h", now=_ts("14:01:40"), last_return_ts=None,
                                 route=APR_FABLE)
    ret = _ts("14:19:41")
    ep = fp.same_error_backoff(prev, err_hash="h", now=ret + 2, last_return_ts=ret,
                               route=("claude-apr", "claude-opus-5-5"))
    assert ep["backoff_s"] is None


def test_fallback_chain_walk_neither_clobbers_nor_counts():
    """primary fails (A) -> fallback #1 fails too (B) -> return -> primary fails (A):
    the walk's failure must not replace the primary's episode."""
    agent = _agent()
    _stash_err = lambda msg: fbe.stash_api_error(  # noqa: E731
        agent, _bad_request(msg), 400, extract_api_error_context(_bad_request(msg)))
    _stash_err(MSG)
    assert fw.same_error_on_failover(agent, failing=APR_FABLE, now=_ts("14:01:40")) == {}
    before = dict(agent._same_err_episode)
    _stash_err("Opus limit reached")  # the fallback's own, different failure
    assert fw.same_error_on_failover(agent, failing=("claude-bpr", "claude-opus-5-5"),
                                     now=_ts("14:01:50")) == {}
    assert agent._same_err_episode == before
    fw.note_primary_return(agent, now=_ts("14:19:41"))
    _stash_err(MSG)
    out = fw.same_error_on_failover(agent, failing=APR_FABLE, now=_ts("14:19:43"))
    assert out["same_err_backoff_s"] == 20 * 60


def test_second_activation_for_one_failure_does_not_inflate_backoff():
    """A second call for the same logical failure (pending error already
    consumed by the row) returns {} and leaves the episode alone."""
    agent = _agent()
    fbe.stash_api_error(agent, _bad_request(MSG), 400, extract_api_error_context(_bad_request(MSG)))
    fw.same_error_on_failover(agent, failing=APR_FABLE, now=_ts("14:01:40"))
    fw.note_primary_return(agent, now=_ts("14:19:41"))
    fbe.stash_api_error(agent, _bad_request(MSG), 400, extract_api_error_context(_bad_request(MSG)))
    first = fw.same_error_on_failover(agent, failing=APR_FABLE, now=_ts("14:19:43"))
    ep = dict(agent._same_err_episode)
    fbe.clear_pending(agent)  # build_row consumed it
    assert fw.same_error_on_failover(agent, failing=APR_FABLE, now=_ts("14:19:44")) == {}
    assert agent._same_err_episode == ep and first["same_err_backoff_s"] == 20 * 60
