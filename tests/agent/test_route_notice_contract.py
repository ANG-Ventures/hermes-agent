"""§4.8 in-chat fallback/return notice contract (same-family fallback spec rev12).

Covers the pure rider layer: the property test (every class x hop x seat
known/unknown x attempts 1/3 carries all four fields), rider/row hop
correctness, the pinned spec examples, the head-label override, and the
recovery rider fields. The AST guard over ``_emit_fallback_announce`` lands
with the announce wiring (it needs ``format_cause_rider`` to be called there).
"""

from __future__ import annotations

import datetime as dt
import itertools
import re

import pytest

from agent import fallback_policy as fp

UTC = dt.timezone.utc


def _ts(h, m, s):
    return dt.datetime(2026, 9, 25, h, m, s, tzinfo=UTC).timestamp()


_CAUSE_TEXT = {
    "conn": "Connection reset by peer",
    "pool_pressure": "pool at capacity",
    "quota_model": "no eligible sub for the requested model",
    "quota_seat": "Claude Fable limit reached",
    "rate_upstream": "exceed your account's rate limit",
    "refusal": "content_policy_blocked",
    "auth": "OAuth access token has been revoked",
    "unclassified": "weird thing",
}
_CAUSE_RE = re.compile(r"[a-zA-Z]{3,}")
_HOP_RE = re.compile(r"(to the relay|at the relay|bridge|\(Anthropic [^)]+\)|proxy|hop \?)")
_SUB_RE = re.compile(r"(sub-vps-\d+|claude-[ab]px-\d+|all subs|sub \?)")
_TIME_RE = re.compile(r"\d\d:\d\d:\d\d(-\d\d(:\d\d:\d\d)?)?$")
_COUNT_RE = re.compile(r"^\d+x ")


def _row(cls, hop, seat, attempts, provider="claude-bpr"):
    return {
        "trigger_class": cls, "hop": hop, "seat": seat, "attempts": attempts,
        "first_err_ts": _ts(14, 2, 11), "last_err_ts": _ts(14, 2, 19),
        "err_head": _CAUSE_TEXT[cls], "http_status": 429, "from_provider": provider,
    }


@pytest.mark.parametrize("cls,hop,seat,attempts", list(itertools.product(
    fp.TRIGGER_CLASSES, fp.HOPS + (None,), ("sub-vps-9", None), (1, 3))))
def test_rider_carries_all_four_fields(cls, hop, seat, attempts):
    rider = fp.format_cause_rider(_row(cls, hop, seat, attempts), tz=UTC)
    assert _CAUSE_RE.search(rider)
    assert _HOP_RE.search(rider), rider
    assert _SUB_RE.search(rider), rider
    assert _TIME_RE.search(rider), rider
    assert bool(_COUNT_RE.match(rider)) is (attempts > 1)
    if hop is None:
        assert "hop ?" in rider
    if seat is None and cls != "quota_model":
        assert "sub ?" in rider


def test_pinned_spec_examples():
    r1 = {"trigger_class": "conn", "hop": "relay->bridge", "seat": "sub-vps-9", "attempts": 3,
          "first_err_ts": _ts(14, 2, 11), "last_err_ts": _ts(14, 2, 19),
          "err_head": "Connection reset by peer"}
    assert fp.format_cause_rider(r1, tz=UTC) == "3x connection reset to sub-vps-9 bridge, 14:02:11-19"
    r2 = {"trigger_class": "quota_model", "hop": "bridge→anthropic", "seat": None, "attempts": 1,
          "first_err_ts": _ts(14, 5, 40), "http_status": 429,
          "err_head": "Claude Fable weekly limit reached", "from_provider": "claude-bpr"}
    assert fp.format_cause_rider(r2, tz=UTC) == (
        "Fable weekly limit (Anthropic 429) on all subs, 14:05:40")
    r3 = {"trigger_class": "quota_model", "hop": "relay", "attempts": 1,
          "first_err_ts": _ts(9, 12, 3), "err_head": "this model's budget is capped",
          "from_provider": "claude-apr"}
    assert fp.format_cause_rider(r3, tz=UTC) == (
        "model budget capped at the relay on all subs, 09:12:03")


def test_relay_ascii_hops_normalize():
    assert fp.normalize_hop("relay->bridge") == "relay→bridge"
    assert fp.normalize_hop("bridge->upstream") == "bridge→anthropic"
    assert fp.normalize_hop("relay") == "relay"
    assert fp.normalize_hop("bogus") is None


def test_hop_agrees_with_row():
    # relay_synthetic rows carry relay / relay→bridge; an unattributable relay
    # response renders hop ?, never bridge→anthropic (pass-4 B4).
    synthetic = fp.format_cause_rider({"trigger_class": "conn", "hop": "relay→bridge",
                                       "relay_synthetic": 1, "seat": "sub-vps-2",
                                       "first_err_ts": _ts(1, 2, 3)}, tz=UTC)
    assert "to sub-vps-2 bridge" in synthetic
    unknown = fp.format_cause_rider({"trigger_class": "unclassified", "hop": None,
                                     "class_source": "text", "seat": None,
                                     "first_err_ts": _ts(1, 2, 3)}, tz=UTC)
    assert "hop ? on sub ?" in unknown and "Anthropic" not in unknown


def test_seat_names_off_renders_a_sub():
    r = fp.format_cause_rider(_row("conn", "relay→bridge", "sub-vps-9", 1), seat_names=False,
                              tz=UTC)
    assert "a sub" in r and "sub-vps-9" not in r


def test_head_label_override_only_for_relay_sourced_transients():
    assert fp.head_label_override({"trigger_class": "conn", "class_source": "relay_header"}) == \
        "connection issue"
    assert fp.head_label_override({"trigger_class": "pool_pressure",
                                   "relay_synthetic": 1}) == "relay busy"
    assert fp.head_label_override({"trigger_class": "conn", "class_source": "text"}) is None
    assert fp.head_label_override({"trigger_class": "quota_seat",
                                   "class_source": "relay_header"}) is None


@pytest.mark.parametrize("branch", ["warm_seat", "fallback_cold", "compaction", "fallback_failed"])
def test_recovery_rider_fields(branch):
    row = {"return_branch": branch, "seat": "sub-vps-6", "since_primary_call_s": 720,
           "expected_warm": branch == "warm_seat", "fallback_idle_s": 64 * 60,
           "dwell_s": 18 * 60, "dwell_turns": 7, "from_model": "claude-opus-5-5",
           "trigger_class": "quota_seat"}
    r = fp.format_recovery_rider(row)
    assert "sub-vps-6" in r and "expected" in r and "after 18m / 7 turns on Opus" in r
    marker = {"warm_seat": "primary eligible", "fallback_cold": "fallback idle 64m",
              "compaction": "compaction", "fallback_failed": "fallback failed"}[branch]
    assert marker in r


def test_mutation_dropping_a_field_is_caught(monkeypatch):
    """Dropping any one field from the rider must turn the property red."""
    row = _row("conn", "relay→bridge", "sub-vps-9", 3)
    monkeypatch.setattr(fp, "_count_window", lambda r, tz: ("", ""))
    assert not _TIME_RE.search(fp.format_cause_rider(row, tz=UTC))
    monkeypatch.undo()
    monkeypatch.setattr(fp, "_seat_token", lambda r, s: "")
    assert not _SUB_RE.search(fp.format_cause_rider(row, tz=UTC))
    monkeypatch.undo()
    monkeypatch.setattr(fp, "_hop_segment", lambda h, s, st: s)
    assert not _HOP_RE.search(fp.format_cause_rider(row, tz=UTC))
