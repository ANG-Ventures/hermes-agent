"""Fallback-events ledger (fallback-cache spec 2026-09-25, Phase 1).

One ``fallback_events`` row per successful ``try_activate_fallback``, with the
§4.1 trigger class. The relay's stated class wins over the text; the status
code lies (connect timeout sent as 429, pool-wide model cap sent as 503) and
must not decide the class; novel text is ``unclassified``; a telemetry failure
never breaks a turn (I3); an error head is scrubbed.
"""

import os
import sqlite3
import types

import pytest

import agent.auxiliary_client as ac
from agent import fallback_events as fbe
from agent.chat_completion_helpers import try_activate_fallback
from agent.error_classifier import FailoverReason
from plugins.blackbox import store

from tests.agent.test_route_change_sink_e2e import (  # noqa: F401
    _fake_agent,
    _patch_resolver,
    _sink_lines,
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("blackbox:\n  enabled: true\n")
    ac.clear_runtime_main()
    try:
        yield tmp_path
    finally:
        ac.clear_runtime_main()


def _rows(home):
    p = os.path.join(str(home), "blackbox", "turns.db")
    if not os.path.exists(p):
        return []
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "select * from fallback_events order by id")]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _agent():
    ac.set_runtime_main("claude-bpr", "claude-fable-5-1",
                        base_url="http://127.0.0.1:18811/anthropic",
                        api_key="primary-key", api_mode="anthropic_messages")
    a = _fake_agent(model="claude-fable-5-1", provider="claude-bpr",
                    base_url="http://127.0.0.1:18811/anthropic",
                    api_mode="anthropic_messages")
    a._current_turn_id = "20260925_120000_abcd:20260925_120000_abcd:t1"
    a.session_id = "20260925_120000_abcd"
    return a


class _Err(Exception):
    def __init__(self, msg, status, headers=None, body=None):
        super().__init__(msg)
        self.status_code = status
        self.response = types.SimpleNamespace(headers=headers or {})
        self.body = body


def _fail_over(monkeypatch, err=None, reason=FailoverReason.rate_limit):
    _patch_resolver(monkeypatch)
    a = _agent()
    if err is not None:
        fbe.stash_api_error(a, err, err.status_code, {"message": str(err)})
    assert try_activate_fallback(a, reason=reason) is True
    return a


# ── one row per successful failover; parity with the sink ──────────────────

def test_one_row_per_successful_failover_matches_sink(_home, monkeypatch):
    _fail_over(monkeypatch, _Err("You've reached your Fable limit", 429,
                                 body={"error": {"message": "Fable limit"}}))
    rows = _rows(_home)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "failover"
    assert (r["from_provider"], r["from_model"]) == ("claude-bpr", "claude-fable-5-1")
    assert (r["to_provider"], r["to_model"]) == ("openai-codex", "gpt-5.5")
    assert r["session_id"] == "20260925_120000_abcd"
    assert r["trigger_class"] == "quota_seat" and r["class_source"] == "text"
    assert r["http_status"] == 429 and r["err_hash"]
    assert len(_sink_lines(str(_home))) == 1


def test_exhausted_chain_writes_no_row(_home, monkeypatch):
    _patch_resolver(monkeypatch)
    a = _agent()
    a._fallback_chain = []
    assert try_activate_fallback(a, reason=FailoverReason.overloaded) is False
    assert _rows(_home) == []


def test_pending_evidence_is_consumed_once(_home, monkeypatch):
    a = _fail_over(monkeypatch, _Err("pool at capacity", 503))
    assert getattr(a, "_pending_fallback_error", None) is None
    assert _rows(_home)[0]["trigger_class"] == "pool_pressure"


# ── §4.1 text table, incl. the status-lying cases ───────────────────────────

@pytest.mark.parametrize("text,status,want", [
    ("upstream connect timed out", 429, "conn"),          # status lies: 429
    ("upstream unreachable on every box", 503, "conn"),
    ("Connection error.", None, "conn"),
    ("upstream attempt timed out", 504, "conn"),
    ("no eligible sub for the requested model; this model's budget is capped "
     "on every subscription", 503, "quota_model"),        # status lies: 503
    ("pool at capacity", 503, "pool_pressure"),
    ("rate limited: the only available subscription is inside its burn-in "
     "window and has reached its per-minute ceiling; retry shortly", 429,
     "pool_pressure"),
    ("capacity 529 ... budget exhausted", 529, "pool_pressure"),
    ("You've reached your Fable limit", 429, "quota_seat"),
    ("You've hit your session limit · resets 2:20pm (UTC)", 429, "quota_seat"),
    ("This request would exceed your account's rate limit", 429, "rate_upstream"),
    ("OAuth access token has been revoked", 401, "auth"),
    ("something the table has never seen", 418, "unclassified"),
    ("", 500, "unclassified"),
])
def test_text_table(text, status, want):
    assert fbe.classify_trigger(text=text, http_status=status)[0] == want


def test_content_policy_reason_is_refusal():
    assert fbe.classify_trigger(text="whatever",
                                reason="content_policy_blocked")[0] == "refusal"


def test_novel_text_never_quota_model():
    for t in ("bench", "weird 503", "limit"):
        assert fbe.classify_trigger(text=t, http_status=503)[0] != "quota_model"


def test_classifier_is_total():
    for t in (None, "", "x" * 5000, "\x00\xff"):
        assert fbe.classify_trigger(text=t)[0] in fbe.TRIGGER_CLASSES


# ── relay header / stream class beats the text ──────────────────────────────

def test_relay_header_beats_text():
    cls, src = fbe.classify_trigger(
        text="You've reached your Fable limit", http_status=429,
        headers={"X-Relay-Error-Class": "conn"})
    assert (cls, src) == ("conn", "relay_header")


def test_relay_stream_class_in_sse_error_json():
    cls, src = fbe.classify_trigger(
        text="pool at capacity", http_status=200,
        body={"type": "error", "error": {"relay_error_class": "quota_seat"}})
    assert (cls, src) == ("quota_seat", "relay_stream")


def test_upstream_passthrough_defers_to_text_and_unknown_is_unclassified():
    assert fbe.classify_trigger(
        text="Fable limit", headers={"x-relay-error-class": "upstream_passthrough"}
    ) == ("quota_seat", "text")
    assert fbe.classify_trigger(
        text="no eligible sub for the requested model",
        headers={"x-relay-error-class": "brand_new_class"}
    ) == ("unclassified", "relay_header")


def test_relay_header_class_reaches_the_row(_home, monkeypatch):
    _fail_over(monkeypatch, _Err("You've reached your Fable limit", 429,
                                 headers={"x-relay-error-class": "quota_model",
                                          "x-pool-route-id": "abc123",
                                          "x-pool-unreachable": "1"}))
    r = _rows(_home)[0]
    assert (r["trigger_class"], r["class_source"]) == ("quota_model", "relay_header")
    assert r["route_id"] == "abc123" and r["relay_synthetic"] == 1


# ── I3: fail-open ───────────────────────────────────────────────────────────

def test_unwritable_db_does_not_raise(_home, monkeypatch):
    def boom(row):
        raise sqlite3.OperationalError("attempt to write a readonly database")
    monkeypatch.setattr(store, "insert_fallback_event", boom)
    _fail_over(monkeypatch, _Err("pool at capacity", 503))   # still True
    assert len(_sink_lines(str(_home))) == 1


def test_blackbox_disabled_writes_nothing(_home, monkeypatch):
    (_home / "config.yaml").write_text("blackbox:\n  enabled: false\n")
    _fail_over(monkeypatch, _Err("pool at capacity", 503))
    assert _rows(_home) == []


# ── negative: token string in a 401 body is redacted ────────────────────────

def test_401_token_redacted_in_err_head(_home, monkeypatch):
    tok = "sk-" + "ant-" + "oat01-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    msg = f"OAuth access token has been revoked: {tok}"
    _fail_over(monkeypatch, _Err(msg, 401, body={"error": {"message": msg}}),
               reason=FailoverReason.auth)
    r = _rows(_home)[0]
    assert r["trigger_class"] == "auth"
    assert r["err_head"] and tok not in r["err_head"]
    assert len(r["err_head"]) <= 160


def test_err_head_only_for_error_json():
    a = types.SimpleNamespace(_current_turn_id="s:s:t", session_id="s")
    fbe.stash_api_error(a, _Err("Connection error.", None), None, None)
    row = fbe.build_row(a, "failover", from_provider="p", from_model="m",
                        to_provider="q", to_model="n")
    assert row["err_head"] is None and row["trigger_class"] == "conn"


# ── next-call back-fill ─────────────────────────────────────────────────────

def test_next_call_cold_backfilled(_home, monkeypatch):
    from agent.usage_pricing import CanonicalUsage
    _fail_over(monkeypatch, _Err("pool at capacity", 503))
    store.insert_api_call(
        "20260925_120000_abcd:20260925_120000_abcd:t1", 1,
        ts=9_999_999_999.0, provider="openai-codex", model="gpt-5.5",
        usage=CanonicalUsage(1000, 50, 0, 40000, 0), sub_key=None,
        attribution="inferred", http_status=200)
    r = _rows(_home)[0]
    assert r["next_call_cold"] == 1 and r["next_call_cache_write"] == 40000
