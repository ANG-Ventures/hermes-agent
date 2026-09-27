"""§4.8 direct-pin seat/hop + ledger class (t_246ce7d6).

Live 2026-09-27 01:15:09 (blackbox fallback_events id=8): a claude-bpx-21
Fable limit rendered ``Fable limit hop ? on sub ?`` and was ledgered as
``quota_seat`` while the sticky writer armed ``quota_model``. A direct pin has
no relay headers (#1260) but its seat and hop are knowable locally.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import types

import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp

UTC = dt.timezone.utc
FABLE_TEXT = ("Claude Code returned an error result: You've reached your Fable limit. "
              "Switch to another model to continue.")


@pytest.fixture
def hosts(tmp_path, monkeypatch):
    """A fleet root holding fleet/hosts.json; HERMES_HOME is a profile under it."""
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / "hosts.json").write_text(json.dumps({"hosts": [
        {"alias": "local", "services": {"claude-bpx": "launchd:com.claude-bpx-0",
                                        "claude-apx": "launchd:ai.agent.claude-apx-0"}},
        {"alias": "ace-ai-lan", "services": {"claude-bpx": "systemd-user:claude-bpx-0.service"}},
        {"alias": "claude-sub-3", "services": {"claude-bpx": "systemd-system:claude-bpx-3.service",
                                               "claude-apx": "systemd-system:claude-apx-3.service"}},
        # alias deliberately NOT derivable by string math from N
        {"alias": "sub-vps-99", "services": {"claude-bpx": "systemd-system:claude-bpx-21.service",
                                             "claude-apx": "systemd-system:claude-apx-21.service"}},
    ]}))
    home = tmp_path / "profiles" / "apollo"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    fp._hosts_cache.update(key=None, map={})
    yield tmp_path
    fp._hosts_cache.update(key=None, map={})


def _row8(**over):
    """fallback_events id=8 as ledgered (hop/seat NULL, first_err_ts NULL)."""
    row = {"ts": dt.datetime(2026, 9, 27, 8, 15, 9, tzinfo=UTC).timestamp(),
           "from_provider": "claude-bpx-21", "from_model": "claude-fable-5-1",
           "to_provider": "claude-bpr", "to_model": "claude-opus-5-5",
           "kind": "failover", "reason": "rate_limit", "trigger_class": "quota_seat",
           "class_source": "text", "http_status": 429, "err_head": FABLE_TEXT[:160],
           "hop": None, "seat": None, "attempts": None, "first_err_ts": None}
    row.update(over)
    return row


def test_row8_replay_renders_seat_and_hop(hosts):
    rider = fp.format_cause_rider(_row8(), tz=UTC)
    assert rider == "Fable limit (Anthropic 429) on sub-vps-99, 08:15:09"
    assert "?" not in rider


def test_seat_resolves_from_hosts_json_not_string_math(hosts):
    assert fp.pin_seat("claude-bpx-21") == "sub-vps-99"
    assert fp.pin_seat("claude-apx-21") == "sub-vps-99"
    assert fp.pin_seat("claude-bpx-3") == "sub-vps-3"      # claude-sub-N normalised
    assert fp.pin_seat("claude-bpx-0") == "local"          # deploy-only ace-ai-lan skipped
    assert fp.pin_seat("claude-apx-0") == "local"
    assert fp.pin_seat("claude-bpx-7") == "sub-vps-7"      # not in registry: name fallback
    assert fp.pin_seat("claude-bpr") is None


@pytest.mark.parametrize("provider,status_hop,conn_hop,status_seg,conn_seg", [
    ("claude-bpx-21", "bridge→anthropic", "client→bridge",
     "(Anthropic 429) on sub-vps-99", "to sub-vps-99 bridge (direct)"),
    ("claude-apx-21", "proxy→anthropic", "client→proxy",
     "(Anthropic 429) via sub-vps-99 proxy", "to sub-vps-99 proxy"),
    ("claude-cpx-21", "cli→anthropic", "client→cli",
     "(Anthropic 429) via sub-vps-99 CLI", "to sub-vps-99 CLI (direct)"),
])
def test_direct_pins_fill_seat_and_hop(hosts, provider, status_hop, conn_hop,
                                       status_seg, conn_seg):
    quota = fp.fill_pin_evidence({"from_provider": provider, "http_status": 429,
                                  "trigger_class": "quota_model"})
    assert (quota["seat"], quota["hop"]) == ("sub-vps-99", status_hop)
    conn = fp.fill_pin_evidence({"from_provider": provider, "http_status": None,
                                 "trigger_class": "conn"})
    assert (conn["seat"], conn["hop"]) == ("sub-vps-99", conn_hop)
    exc = fp.fill_pin_evidence({"from_provider": provider, "trigger_class": "unclassified"},
                               exc_name="APITimeoutError")
    assert exc["hop"] == conn_hop
    t = dt.datetime(2026, 9, 27, 1, 2, 3, tzinfo=UTC).timestamp()
    assert fp.format_cause_rider({"from_provider": provider, "http_status": 429,
                                  "trigger_class": "quota_model", "err_head": FABLE_TEXT,
                                  "ts": t}, tz=UTC) == f"Fable limit {status_seg}, 01:02:03"
    assert fp.format_cause_rider({"from_provider": provider, "trigger_class": "conn",
                                  "err_head": "Connection reset by peer", "ts": t},
                                 tz=UTC) == f"connection reset {conn_seg}, 01:02:03"


def test_pin_evidence_never_overrides_recorded_fields(hosts):
    row = fp.fill_pin_evidence({"from_provider": "claude-bpx-21", "http_status": 429,
                                "seat": "sub-vps-5", "hop": "client→bridge"})
    assert (row["seat"], row["hop"]) == ("sub-vps-5", "client→bridge")


def test_pooled_unknown_stays_unknown_in_words(hosts):
    t = dt.datetime(2026, 9, 27, 1, 18, 5, tzinfo=UTC).timestamp()
    row = {"from_provider": "claude-bpr", "trigger_class": "quota_seat", "class_source": "text",
           "http_status": 429, "err_head": FABLE_TEXT, "ts": t}
    assert fp.fill_pin_evidence(row) == row
    assert fp.format_cause_rider(row, tz=UTC) == "Fable limit (hop unknown, sub unknown), 01:18:05"
    known_seat = dict(row, seat="sub-vps-6")
    assert fp.format_cause_rider(known_seat, tz=UTC) == (
        "Fable limit on sub-vps-6 (hop unknown), 01:18:05")
    known_hop = dict(row, hop="relay")
    assert fp.format_cause_rider(known_hop, tz=UTC) == (
        "Fable limit at the relay (sub unknown), 01:18:05")


def _agent(provider, *, status, text, exc="RateLimitError"):
    return types.SimpleNamespace(
        provider=provider, session_id="s1", _current_turn_id="s1:t1",
        _pending_fallback_error={"at": time.monotonic(), "status": status, "text": text,
                                 "headers": {}, "body": {"error": {"message": text}},
                                 "exc": exc})


def test_ledger_row_live_path_direct_pin(hosts):
    """The live path (class_source=text, no stated reset) ledgers the class the
    sticky writer armed (quota_model) and fills seat + hop."""
    agent = _agent("claude-bpx-21", status=429, text=FABLE_TEXT)
    row = fbe.build_row(agent, "failover", from_provider="claude-bpx-21",
                        from_model="claude-fable-5-1", to_provider="claude-bpr",
                        to_model="claude-opus-5-5", reason="rate_limit")
    assert row["class_source"] == "text"
    assert row["trigger_class"] == "quota_model"
    assert (row["seat"], row["hop"]) == ("sub-vps-99", "bridge→anthropic")


def test_ledger_row_pooled_keeps_quota_seat_and_unknowns(hosts):
    agent = _agent("claude-bpr", status=429, text=FABLE_TEXT)
    row = fbe.build_row(agent, "failover", from_provider="claude-bpr",
                        from_model="claude-fable-5-1", to_provider="claude-bpr",
                        to_model="claude-opus-5-5", reason="rate_limit")
    assert row["trigger_class"] == "quota_seat"
    assert row.get("seat") is None and row.get("hop") is None


V2_HEADERS = {"x-relay-error-class": "quota_seat", "x-relay-error-hop": "relay",
              "x-relay-seat": "sub-vps-7", "x-relay-seats-tried": "3",
              "x-relay-eligible": "4"}


def _v2_agent(provider, headers, body=None, *, status=429, text=FABLE_TEXT):
    err = types.SimpleNamespace(
        response=types.SimpleNamespace(headers=headers),
        body=body if body is not None else {"error": {"message": text}})
    agent = types.SimpleNamespace(provider=provider, session_id="s1",
                                  _current_turn_id="s1:t1")
    fbe.stash_api_error(agent, err, status, {"message": text})
    return agent


def _pooled_row(agent):
    return fbe.build_row(agent, "failover", from_provider="claude-bpr",
                         from_model="claude-fable-5-1", to_provider="claude-bpr",
                         to_model="claude-opus-5-5", reason="rate_limit")


def test_ledger_row_pooled_with_v2_headers_fills_seat_and_hop(hosts):
    """t_dbdd08c3: the relay's x-relay-error-hop / x-relay-seat survive the
    stash and land on the pooled row, so the notice names hop and sub."""
    row = _pooled_row(_v2_agent("claude-bpr", V2_HEADERS))
    assert row["class_source"] == "relay_header"
    assert row["trigger_class"] == "quota_seat"
    assert (row["seat"], row["hop"]) == ("sub-vps-7", "relay")
    rider = fp.format_cause_rider(row, tz=UTC)
    assert "at the relay on sub-vps-7" in rider
    assert "unknown" not in rider


@pytest.mark.parametrize("raw,enum", [("relay->bridge", "relay→bridge"),
                                      ("bridge->upstream", "bridge→anthropic")])
def test_v2_hop_ascii_normalized_to_notice_enum(hosts, raw, enum):
    row = _pooled_row(_v2_agent("claude-bpr", dict(V2_HEADERS, **{"x-relay-error-hop": raw})))
    assert row["hop"] == enum


@pytest.mark.parametrize("seat", ["none", "unknown", ""])
def test_v2_unstated_seat_stays_sub_unknown(hosts, seat):
    """The relay sends ``none`` when no seat was tried; that is not a seat."""
    row = _pooled_row(_v2_agent("claude-bpr", dict(V2_HEADERS, **{"x-relay-seat": seat})))
    assert row.get("seat") is None
    assert fp.format_cause_rider(row, tz=UTC).endswith(
        "at the relay (sub unknown), " + fp._hms(row["ts"], UTC).strftime("%H:%M:%S"))


@pytest.mark.parametrize("nested", [False, True])
def test_v2_stream_error_body_fills_seat_and_hop(hosts, nested):
    """Stream-phase failure: no v2 headers, the relay fields ride the SSE
    error event (Anthropic top level, or inside ``error`` for OpenAI SDK)."""
    fields = {"relay_error_class": "quota_seat", "relay_error_hop": "relay->bridge",
              "relay_seat": "sub-vps-8", "relay_seats_tried": 2}
    body = ({"error": dict({"message": FABLE_TEXT}, **fields)} if nested
            else dict({"type": "error", "error": {"message": FABLE_TEXT}}, **fields))
    row = _pooled_row(_v2_agent("claude-bpr", {}, body))
    assert row["class_source"] == "relay_stream"
    assert (row["seat"], row["hop"]) == ("sub-vps-8", "relay→bridge")
    assert "to sub-vps-8 bridge" in fp.format_cause_rider(row, tz=UTC)


def test_v2_headers_never_override_recorded_fields(hosts):
    agent = _v2_agent("claude-bpr", V2_HEADERS)
    row = fbe.build_row(agent, "failover", from_provider="claude-bpr",
                        from_model="claude-fable-5-1", to_provider="claude-bpr",
                        to_model="claude-opus-5-5", reason="rate_limit",
                        extra={"seat": "sub-vps-5", "hop": "client→relay"})
    assert (row["seat"], row["hop"]) == ("sub-vps-5", "client→relay")


def test_live_fable_text_on_pin_uses_class_default_cooldown():
    """The live text carries no reset: the pin's quota_model cooldown is the
    class default (6h base x jitter; row 8 = 22461 s = 6h x 1.04), not a
    stated reset. A stated reset, when present, wins (see the 7d test)."""
    cls, window = fp.lane_class("quota_seat", "claude-bpx-21", FABLE_TEXT)
    assert (cls, window) == ("quota_model", None)
    assert fp.parse_stated_reset_s(FABLE_TEXT, 0.0) is None
    assert fp.compute_cooldown_s(cls, 0, stated_reset_s=None, window=window,
                                 jitter=1.04) == pytest.approx(6 * 3600 * 1.04)
