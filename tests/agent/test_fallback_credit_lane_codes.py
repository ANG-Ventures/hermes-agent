"""t_4c447d32: the credit-lane relay (claude-pool ``credit_lane_relay.py``, face
claude-elr, :18821) answers in the Anthropic error shape with its OWN
``error.type`` values. On 2026-11-18 21:28 three sessions got

    🔄 Model fallback (provider error): claude-elr/claude-fable-5-2 → claude-dtlr/claude-fable-5-2
    — unclassified error (hop unknown, relay sent no seat), 21:28:36

for a 502 whose body stated the cause and the seat (dead-letter err_hash
0252b0fcf1). Every code the relay emits through ``_err(...)`` is pinned to one
table row (``fallback_events.CREDIT_LANE_ERROR_CODES``), and the live body is
replayed through the real failover + renderer.
"""

from __future__ import annotations

import datetime as dt
import time
import types

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401

UTC = dt.timezone.utc
TS = dt.datetime(2026, 11, 19, 5, 28, 36, tzinfo=UTC).timestamp()   # 21:28:36 PST

# claude-pool credit_lane_relay.py / credit_lane.body_refusal at 5fed23d0d5:
# every ``error.type`` the relay writes through ``_err(...)`` that is its own
# (not a generic HTTP name). Pinned here so a new relay code is a red test,
# not an "unclassified" banner. credit_upstream_write_failed: claude-pool#470.
RELAY_CREDIT_CODES = {
    "credit_upstream_read_failed": 502, "credit_upstream_write_failed": 502,
    "credit_generation_changed": 502,
    "credit_org_mismatch": 502, "credit_lane_exhausted": 503, "credit_lane_off": 503,
    "credit_lane_cc_body": 400, "credit_lane_fable_off": 400,
}

LIVE_BODY = {"type": "error", "error": {
    "type": "credit_upstream_read_failed",
    "message": "upstream read failed after send on sub-vps-12 (TimeoutError); not retried"}}
EXPECTED = ("🔄 Model fallback (seat timed out): claude-elr/claude-fable-5-2 → "
            "claude-dtlr/claude-fable-5-2 — no answer from Anthropic through "
            "sub-vps-12 (TimeoutError), request was sent, not re-sent, 21:28:36")

# claude-pool#470: the upload from the Studio to the seat's box did not finish.
WRITE_BODY = {"type": "error", "error": {
    "type": "credit_upstream_write_failed",
    "message": ("request upload failed on sub-vps-12 (TimeoutError) after 120 s of a 120 s "
                "limit with 1.7 MB to send; request not fully sent, not retried")}}
WRITE_EXPECTED = ("🔄 Model fallback (upload timed out): claude-elr/claude-fable-5-2 → "
                  "claude-dtlr/claude-fable-5-2 — our upload to sub-vps-12 did not finish "
                  "in 120 s (1.7 MB, Studio to box link; ours, not Anthropic), not re-sent, "
                  "21:28:36")


def _pst(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()


def _elr_failover(monkeypatch, err, reason):
    import agent.auxiliary_client as ac
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_events_ledger import _patch_resolver
    from tests.agent.test_route_change_sink_e2e import _fake_agent

    _patch_resolver(monkeypatch)
    monkeypatch.setattr(fbe, "time", types.SimpleNamespace(
        time=lambda: TS, monotonic=time.monotonic, strftime=time.strftime))
    base = "http://127.0.0.1:18821/elr"
    ac.set_runtime_main("claude-elr", "claude-fable-5-2", base_url=base,
                        api_key="primary-key", api_mode="anthropic_messages")
    a = _fake_agent(model="claude-fable-5-2", provider="claude-elr", base_url=base,
                    api_mode="anthropic_messages")
    a._fallback_chain = [{"provider": "claude-dtlr", "model": "claude-fable-5-2"}]
    a.fallback_model = list(a._fallback_chain)
    a.session_id = "20261111_222101_a4b76071"
    a._current_turn_id = f"{a.session_id}:{a.session_id}:t1"
    fbe.stash_api_error(a, err, err.status_code)
    assert try_activate_fallback(a, reason=reason) is True
    return a


def _credit_err(code, status, message=None, headers=None):
    from tests.agent.test_fallback_rider_truth import _Err

    body = {"type": "error", "error": {"type": code, "message": message or code}}
    return _Err(body, status, headers or {})


# ── the table covers every relay code ──────────────────────────────────────

def test_every_relay_credit_code_has_a_row():
    missing = sorted(set(RELAY_CREDIT_CODES) - set(fbe.CREDIT_LANE_ERROR_CODES))
    assert not missing, f"credit-lane relay codes with no table row: {missing}"
    extra = sorted(set(fbe.CREDIT_LANE_ERROR_CODES) - set(RELAY_CREDIT_CODES))
    assert not extra, f"table rows the relay does not emit: {extra}"


@pytest.mark.parametrize("code", sorted(RELAY_CREDIT_CODES))
def test_row_shape(code):
    row = fbe.CREDIT_LANE_ERROR_CODES[code]
    assert row.trigger_class in fbe.TRIGGER_CLASSES and row.trigger_class != "unclassified"
    assert 2 <= len(row.head.split()) <= 3, row.head
    assert fp.normalize_hop(row.hop) in fp.HOPS, row.hop


@pytest.mark.parametrize("code,status", sorted(RELAY_CREDIT_CODES.items()))
def test_classify_trigger_ignores_the_table(code, status):
    # Labels only (card rail; Prism 16f26d03b5d4): classify_trigger feeds the
    # sticky arm and the quota gate through pending_trigger_class, so a credit
    # code must classify exactly as the same body with a non-credit type does.
    def _cls(etype):
        body = {"type": "error", "error": {"type": etype, "message": code}}
        return fbe.classify_trigger(text=f"HTTP {status}: {body}", http_status=status,
                                    headers={}, body=body)

    assert _cls(code) == _cls("api_error")


def test_unknown_error_type_is_not_a_credit_code():
    body = {"type": "error", "error": {"type": "api_error", "message": "Internal server error"}}
    assert fbe.credit_lane_code(body) is None
    assert fbe.credit_lane_code({"error": "pool at capacity"}) is None
    assert fbe.credit_lane_code(None, "garbage") is None
    # A credit_* string in the text alone is not the relay's answer.
    assert fbe.credit_lane_code(None, '{"type": "credit_lane_off"}') is None


def test_seat_and_exception_read_from_the_message():
    assert fbe.credit_lane_seat(LIVE_BODY) == "sub-vps-12"
    assert fbe.credit_lane_exc(LIVE_BODY) == "TimeoutError"
    org = {"error": {"type": "credit_org_mismatch",
                     "message": "response org did not match the ledger org for local"}}
    assert fbe.credit_lane_seat(org) == "local"
    bare = {"error": {"type": "credit_upstream_read_failed",
                      "message": "credit_upstream_read_failed"}}
    assert fbe.credit_lane_seat(bare) is None and fbe.credit_lane_exc(bare) is None
    assert fbe.credit_lane_upload(WRITE_BODY) == ("120", "1.7")
    assert fbe.credit_lane_upload(bare) == (None, None)


# ── the live 21:28:36 row, end to end ──────────────────────────────────────

def test_live_dead_letter_body_renders_the_target_banner(_home, monkeypatch):
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_rider_truth import _Err

    _pst(monkeypatch)
    try:
        _elr_failover(monkeypatch, _Err(LIVE_BODY, 502, {}), FailoverReason.server_error)
        row = _rows(_home)[0]
        text = row["notice_text"]
        print(text)
        assert text == EXPECTED, text
        assert "unclassified" not in text and "hop unknown" not in text
        assert row["class_source"] != "relay_code"   # the policy saw what it saw before
        assert not (_home / "state" / "fallback-unclassified.jsonl").exists()
    finally:
        monkeypatch.undo()
        time.tzset()


def test_upload_failed_body_renders_the_target_banner(_home, monkeypatch):
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_rider_truth import _Err

    _pst(monkeypatch)
    try:
        _elr_failover(monkeypatch, _Err(WRITE_BODY, 502, {}), FailoverReason.server_error)
        row = _rows(_home)[0]
        text = row["notice_text"]
        print(text)
        assert text == WRITE_EXPECTED, text
        assert "ours, not Anthropic" in text
        assert row["hop"] == "relay→bridge"   # the relay-to-box leg, not proxy→anthropic
        assert not (_home / "state" / "fallback-unclassified.jsonl").exists()
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("code,status,message,head,rider", [
    ("credit_upstream_read_failed", 502,
     "upstream read failed after send on sub-vps-3 (ConnectionResetError); not retried",
     "seat read failed",
     "no answer from Anthropic through sub-vps-3 (ConnectionResetError), request was sent, "
     "not re-sent"),
    ("credit_upstream_write_failed", 502,
     "request upload failed on sub-vps-4 (ConnectionResetError) after 3 s of a 120 s limit; "
     "request not fully sent, not retried",
     "upload failed",
     "our upload to sub-vps-4 did not finish, failed after 3 s of 120 s (Studio to box link; "
     "ours, not Anthropic), not re-sent"),
    ("credit_upstream_write_failed", 502,
     "request upload failed on sub-vps-4 (TimeoutError) with 0.5 MB to send; not retried",
     "upload timed out",
     "our upload to sub-vps-4 did not finish (0.5 MB, Studio to box link; ours, not "
     "Anthropic), not re-sent"),
    ("credit_upstream_write_failed", 502, None, "upload failed",
     "our upload to the seat's box did not finish (Studio to box link; ours, not "
     "Anthropic), not re-sent"),
    ("credit_upstream_read_failed", 502, None, "seat read failed",
     "relay could not read Anthropic's answer (relay sent no seat), not re-sent"),
    ("credit_generation_changed", 502, None, "credit key changed",
     "credit key changed mid-request at the relay (ours, not Anthropic), not re-sent"),
    ("credit_org_mismatch", 502, "response org did not match the ledger org for sub-vps-7",
     "credit org mismatch",
     "answer came from the wrong org on sub-vps-7 (ours, not Anthropic), key quarantined"),
    ("credit_lane_exhausted", 503, "no eligible credit key (exhausted); tried ['sub-vps-1:exhausted']",
     "no credit key", "no eligible credit key at the relay (exhausted)"),
    ("credit_lane_off", 503, "credit lane disabled (credit-lane.json enabled:false)",
     "credit lane off", "credit lane disabled at the relay (credit-lane.json)"),
    ("credit_lane_cc_body", 400, None, "lane refused request",
     "credit lane does not serve Claude Code requests (ours, not Anthropic)"),
    ("credit_lane_fable_off", 400, None, "Fable disabled",
     "credit lane has Fable switched off (ours, not Anthropic)"),
])
def test_each_code_renders_named_head_and_rider(_home, monkeypatch, code, status, message,
                                                head, rider):
    from agent.error_classifier import FailoverReason

    _pst(monkeypatch)
    try:
        _elr_failover(monkeypatch, _credit_err(code, status, message),
                      FailoverReason.server_error)
        text = _rows(_home)[0]["notice_text"]
        assert text == (f"🔄 Model fallback ({head}): claude-elr/claude-fable-5-2 → "
                        f"claude-dtlr/claude-fable-5-2 — {rider}, 21:28:36"), text
        assert "unclassified" not in text and "hop unknown" not in text
        assert not (_home / "state" / "fallback-unclassified.jsonl").exists()
    finally:
        monkeypatch.undo()
        time.tzset()


def test_seat_names_off_hides_the_seat():
    row = {"trigger_class": "conn", "class_source": "relay_code", "from_provider": "claude-elr",
           "credit_code": "credit_upstream_read_failed", "seat": "sub-vps-12",
           "credit_exc": "TimeoutError", "hop": "relay", "http_status": 502, "ts": TS}
    text = fp.format_cause_rider(row, seat_names=False, tz=UTC)
    assert text == ("no answer from Anthropic through a sub (TimeoutError), "
                    "request was sent, not re-sent, 05:28:36"), text


def test_pool_relay_string_error_is_untouched():
    # Negative control: the pool relay's {"error": "<string>"} shape keeps its own path.
    body = {"error": "upstream attempt timed out"}
    assert fbe.credit_lane_code(body) is None
    assert fbe.relay_synthetic_error(body) == "upstream attempt timed out"


# ── t_2ff17f3d: Anthropic's credit-balance refusal, passed through in-stream ──
# Dead-letter err_hash 983ab2574f (13 banners 11-19 07:20..11-20 07:20): the
# credit-lane relay had already answered HTTP 200 and relayed Anthropic's own
# error event. The runtime classified it billing ("credit exhausted"), the
# ledger row unclassified, so the banner read "credit exhausted ... unclassified
# error on credit:sub-vps-13 (hop unknown)". One error, one cause.
CREDIT_BALANCE_BODY = {"type": "error", "error": {
    "details": None, "type": "invalid_request_error",
    "message": ("Your credit balance is too low to access the Anthropic API. Please go to "
                "Plans & Billing to upgrade or purchase credits.")},
    "request_id": "req_011ChEuD3eFkSKHmStLpcuDY"}
CREDIT_BALANCE_EXPECTED = (
    "🔄 Model fallback (credit exhausted): claude-elr/claude-fable-5-2 → "
    "claude-dtlr/claude-fable-5-2 — out of credits (Anthropic in-stream error) via "
    "credit:sub-vps-13 proxy, 21:28:36")


def test_in_stream_credit_balance_refusal_label_and_detail_agree(_home, monkeypatch):
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_rider_truth import _Err

    _pst(monkeypatch)
    try:
        err = _Err(CREDIT_BALANCE_BODY, 200, {"x-pool-served-by": "credit:sub-vps-13"})
        _elr_failover(monkeypatch, err, FailoverReason.billing)
        row = _rows(_home)[0]
        text = row["notice_text"]
        print(text)
        assert text == CREDIT_BALANCE_EXPECTED, text
        assert "unclassified" not in text and "hop unknown" not in text
        assert row["hop"] == "proxy→anthropic"
        # Labels only: the policy class is what it was before (no sticky/quota change).
        assert row["trigger_class"] == "unclassified"
        assert not (_home / "state" / "fallback-unclassified.jsonl").exists()
    finally:
        monkeypatch.undo()
        time.tzset()


def test_credit_balance_message_is_one_cause_class():
    # Label (FailoverReason.billing -> "credit exhausted") and detail must name
    # the same cause class for the recorded shape.
    from agent.chat_completion_helpers import _FALLBACK_ANNOUNCE_LABELS

    row = {"trigger_class": "unclassified", "class_source": "text", "from_provider": "claude-elr",
           "seat": "credit:sub-vps-13", "hop": "proxy→anthropic", "http_status": 200, "ts": TS,
           "err_head": CREDIT_BALANCE_BODY["error"]["message"]}
    detail = fp.format_cause_rider(row, tz=UTC)
    assert detail.startswith(fp.CREDITS_CAUSE), detail
    assert "credit" in _FALLBACK_ANNOUNCE_LABELS["billing"]
