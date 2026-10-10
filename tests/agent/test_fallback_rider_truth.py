"""t_6eddafcd: fallback riders say what actually happened.

Two lines from 2026-10-03 (session 20261003_194901_3ef14583) were accurate
but unreadable:

* 21:18:26 alr -> dtlr: ``empty response (stop_reason=tool_use, 0 content
  blocks, 128 out) · hop=relay-200 · sub=sub-vps-23``. The relay had already
  re-sent the turn three times (sub-vps-18 x2, sub-vps-23), all billed-empty.
* 21:25:28 dtlr -> btpr: ``(pool sub stalled mid-turn) ... read timeout on
  sub-vps-18 (hop unknown)``. A relay 504 ``upstream attempt timed out``
  after 7m00s, before the first byte, on a lane with no other eligible seat.

Both rendered lines are pinned here through the real failover + renderer,
with negative controls, plus a table-driven contract: no relay-synthetic body
renders ``hop unknown``; a body that matches no row still does.
"""

from __future__ import annotations

import datetime as dt

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401

UTC = dt.timezone.utc
TS = dt.datetime(2026, 10, 4, 4, 25, 28, tzinfo=UTC).timestamp()


# ── pure renderer: the two live shapes ───────────────────────────────────

def _empty_row(**floor):
    fl = {"site": "invalid_response", "stop_reason": "tool_use", "content_blocks": 0,
          "output_tokens": 128, "served_by": "sub-vps-23",
          "route_id": "054fb478cf644ad99659e051e75c5620"}
    fl.update(floor)
    return {"trigger_class": fp.INVALID_RESPONSE_CLASS, "from_provider": "claude-alr",
            "seat": fl.get("served_by"), "floor": fl, "ts": TS}


def test_empty_reply_relay_gave_up_names_the_attempt_chain():
    row = _empty_row(relay_retry="gave_up",
                     relay_attempts=["sub-vps-18", "sub-vps-18", "sub-vps-23"])
    assert fp.format_cause_rider(row, tz=UTC) == (
        "empty tool-call reply ×3 from sub-vps-18 (stop_reason=tool_use, 0 content blocks,"
        " ~130 out) — relay retried ×3 across sub-vps-18, sub-vps-23, gave up, 04:25:28")
    assert fp.head_label_override(row) == "empty tool-call reply ×3"


def test_empty_reply_relay_gave_up_without_seat_list_names_last_seat():
    row = _empty_row(relay_retry="gave_up")
    assert fp.format_cause_rider(row, tz=UTC) == (
        "empty tool-call reply from sub-vps-23 (stop_reason=tool_use, 0 content blocks,"
        " ~130 out) — relay retried, gave up, 04:25:28")
    assert fp.head_label_override(row) == "empty tool-call reply, relay retried"


def test_empty_reply_chat_line_drops_raw_stop_reason():
    """Without a relay give-up the raw stop_reason/blocks/out stays in the log
    row, never the chat. A relay give-up names the reply shape (t_9783560a)."""
    text = fp.format_cause_rider(_empty_row(), tz=UTC)
    assert "stop_reason=" not in text and "content block" not in text, text
    for row in (_empty_row(), _empty_row(relay_retry="gave_up"),
                _empty_row(relay_retry="gave_up", relay_attempts=["sub-vps-2"])):
        text = fp.format_cause_rider(row, tz=UTC)
        assert "hop unknown" not in text and "sub unknown" not in text, text
        assert "from Anthropic" not in text, text
    # The ledger/log cause still carries the raw evidence.
    assert fp.invalid_response_cause(_empty_row()) == (
        "empty response (stop_reason=tool_use, 0 content blocks, 128 out)")


def test_empty_reply_without_relay_retry_keeps_hop_and_seat():
    assert fp.format_cause_rider(_empty_row(), tz=UTC) == (
        "empty reply · hop=relay-200 · sub=sub-vps-23, 04:25:28")
    assert fp.head_label_override(_empty_row()) == "empty reply"


_LADDER_RIDS = ["req_011CfjmrBqPSu3XP0Aeizj", "req_011CfjmsQRi8ScuyUPEnTq",
                "req_011CfjmtY76qe92qjndaQa"]
CARD_SAMPLE = (
    "empty tool-call reply ×3 from sub-vps-22 (stop_reason=tool_use, 0 content blocks,"
    " ~240 out): as-is, −fgts beta, rotate→sub-vps-9 −fgts; req_…Aeizj, req_…UPEnTq,"
    " req_…jndaQa; prompt 203k tok — relay ladder exhausted, falling to next lane")


def _ladder_row(**floor):
    """The 2026-10-08 01:25 give-up: three billed empties, 243 out, ~203k prompt."""
    kw = dict(relay_retry="gave_up", served_by="sub-vps-22", output_tokens=243,
              relay_attempts=["sub-vps-22", "sub-vps-22", "sub-vps-22"],
              relay_request_ids=list(_LADDER_RIDS), prompt_tokens=203_400)
    kw.update(floor)
    return _empty_row(**kw)


def test_ladder_with_perturbations_renders_the_card_line():
    row = _ladder_row(relay_attempts=["sub-vps-22", "sub-vps-22", "sub-vps-9"],
                      relay_perturbations=["none", "drop_fgts", "drop_fgts+rotate"])
    assert fp.format_cause_rider(row, tz=UTC) == CARD_SAMPLE.replace(
        "req_…Aeizj", "req_…0Aeizj") + ", 04:25:28"
    assert fp.head_label_override(row) == "empty tool-call reply ×3"


def test_same_seat_ladder_collapses_to_one_seat():
    row = _ladder_row(relay_perturbations=["none", "drop_fgts", "drop_fgts"])
    text = fp.format_cause_rider(row, tz=UTC)
    assert text == (
        "empty tool-call reply ×3 from sub-vps-22 (stop_reason=tool_use, 0 content blocks,"
        " ~240 out): as-is, −fgts beta, −fgts beta; req_…0Aeizj, req_…UPEnTq, req_…jndaQa;"
        " prompt 203k tok — relay ladder exhausted, falling to next lane, 04:25:28"), text
    assert "sub-vps-22," not in text and "from Anthropic" not in text


def test_rotate_rung_names_the_new_seat_inline():
    row = _ladder_row(relay_attempts=["sub-vps-22", "sub-vps-22", "sub-vps-9"],
                      relay_perturbations=["none", "drop_fgts", "drop_fgts+rotate"])
    text = fp.format_cause_rider(row, tz=UTC)
    assert text.startswith("empty tool-call reply ×3 from sub-vps-22 ("), text
    assert ": as-is, −fgts beta, rotate→sub-vps-9 −fgts; req_…" in text, text


def test_rung_names_map_and_unknown_kinds_print_verbatim():
    row = _ladder_row(relay_attempts=["sub-vps-22", "sub-vps-22", "sub-vps-7"],
                      relay_perturbations=["none", "new_thing", "rotate"],
                      relay_request_ids=None, prompt_tokens=None)
    text = fp.format_cause_rider(row, tz=UTC)
    assert ": as-is, new_thing, rotate→sub-vps-7 — relay ladder exhausted" in text, text


def test_perturb_prompt_rungs_map_to_labels_not_raw_tokens():
    """claude-pool#223 tokens render as labels, never raw (t_0256b47c)."""
    row = _ladder_row(relay_attempts=["local", "local", "sub-vps-11"],
                      relay_perturbations=["none", "perturb_prompt", "perturb_prompt+rotate"],
                      relay_request_ids=None, prompt_tokens=300_148)
    text = fp.format_cause_rider(row, tz=UTC)
    assert ": as-is, +\\n prompt, rotate→sub-vps-11 +\\n prompt; prompt 300k tok" in text, text
    assert "perturb_prompt" not in text and text.count("rotate") == 1, text


def test_without_perturbation_header_same_seat_says_same_seat():
    text = fp.format_cause_rider(_ladder_row(), tz=UTC)
    assert text == (
        "empty tool-call reply ×3 from sub-vps-22 (stop_reason=tool_use, 0 content blocks,"
        " ~240 out); req_…0Aeizj, req_…UPEnTq, req_…jndaQa; prompt 203k tok"
        " — relay retried same seat ×3, gave up, 04:25:28"), text


def test_end_turn_empty_is_a_plain_empty_reply():
    row = _ladder_row(stop_reason="end_turn")
    assert fp.head_label_override(row) == "empty reply ×3"
    text = fp.format_cause_rider(row, tz=UTC)
    assert text.startswith("empty reply ×3 from sub-vps-22 (stop_reason=end_turn, "), text
    assert "tool-call" not in text


def test_group_chat_line_names_no_seat():
    for row in (_ladder_row(relay_perturbations=["none", "drop_fgts", "drop_fgts+rotate"],
                            relay_attempts=["sub-vps-22", "sub-vps-22", "sub-vps-9"]),
                _ladder_row(), _ladder_row(relay_attempts=["sub-vps-22", "sub-vps-9"]),
                _ladder_row(relay_attempts=None)):
        text = fp.format_cause_rider(row, tz=UTC, seat_names=False)
        assert "sub-vps" not in text, text
        assert "a sub" in text, text


def test_length_cap_drops_request_ids_first():
    long_rids = [f"req_{i:02d}" + "x" * 30 for i in range(8)]
    row = _ladder_row(relay_attempts=["sub-vps-22", "sub-vps-22", "sub-vps-9"],
                      relay_perturbations=["none", "drop_fgts", "drop_fgts+rotate"],
                      relay_request_ids=long_rids)
    text = fp.format_cause_rider(row, tz=UTC)
    assert "req_…" not in text, text
    assert "rotate→sub-vps-9 −fgts; prompt 203k tok — relay ladder exhausted" in text, text
    assert len(text[:-len(", 04:25:28")]) <= fp.RELAY_EMPTY_CHAIN_MAX, (len(text), text)
    # The card's own three-id line fits and keeps its ids.
    assert "req_…" in fp.format_cause_rider(_ladder_row(), tz=UTC)


def test_non_empty_invalid_response_keeps_its_cause():
    row = _empty_row(content_blocks=None, stop_reason=None, output_tokens=None,
                     detail="response.choices missing")
    assert "invalid response" in fp.format_cause_rider(row, tz=UTC)
    assert fp.head_label_override(row) is None


def _seat_timeout_row(**kw):
    row = {"trigger_class": "conn", "from_provider": "claude-dtlrf", "hop": "relay→bridge",
           "seat": "sub-vps-18", "http_status": 504, "relay_error": "upstream attempt timed out",
           "err_head": 'HTTP 504: {"error":"upstream attempt timed out"}',
           "elapsed_s": 420.0, "pool_eligible": 0, "ts": TS}
    row.update(kw)
    return row


def test_seat_timeout_names_seat_elapsed_and_lane_wide():
    row = _seat_timeout_row()
    assert fp.format_cause_rider(row, tz=UTC) == (
        "sub-vps-18 did not answer in 7m00s (relay deadline) · pool had no other seat, 04:25:28")
    assert fp.head_label_override(row) == "seat timed out"


def test_seat_timeout_other_seats_existed_has_no_pool_rider():
    text = fp.format_cause_rider(_seat_timeout_row(pool_eligible=2), tz=UTC)
    assert text == "sub-vps-18 did not answer in 7m00s (relay deadline), 04:25:28"
    text = fp.format_cause_rider(_seat_timeout_row(pool_eligible=None), tz=UTC)
    assert "pool had no other seat" not in text


def test_seat_timeout_unknown_seat_and_elapsed():
    text = fp.format_cause_rider(_seat_timeout_row(seat=None, elapsed_s=None,
                                                   pool_eligible=None), tz=UTC)
    assert text == "the seat did not answer before the relay deadline, 04:25:28"
    assert "read timeout" not in text and "hop unknown" not in text


def test_read_timeout_only_for_a_client_side_read_timeout():
    base = {"trigger_class": "conn", "from_provider": "claude-bpr", "hop": "relay→bridge",
            "seat": "sub-vps-9", "err_head": "Request timed out.", "ts": TS}
    assert fp.format_cause_rider(dict(base, exc_name="ReadTimeout"), tz=UTC).startswith(
        "read timeout to sub-vps-9 bridge")
    assert fp.format_cause_rider(dict(base, socket_cause="read_timeout"), tz=UTC).startswith(
        "read timeout")
    # Same text, no client read-timeout evidence: never "read timeout".
    assert fp.format_cause_rider(base, tz=UTC).startswith("timed out to sub-vps-9 bridge")


def test_pool_deadline_504_is_not_a_read_timeout():
    row = _seat_timeout_row(relay_error="pool deadline exceeded",
                            err_head='{"error":"pool deadline exceeded"}')
    text = fp.format_cause_rider(row, tz=UTC)
    assert text.startswith("relay deadline exceeded to sub-vps-18 bridge"), text
    assert text.endswith("· pool had no other seat, 04:25:28")
    assert fp.head_label_override(row) == "relay deadline"


# ── table contract: every relay-synthetic body names its hop ─────────────

@pytest.mark.parametrize("error", sorted(fbe.RELAY_SYNTHETIC_HOP))
def test_every_relay_synthetic_body_renders_a_known_hop(error):
    """A relay body with NO x-relay-error-hop header (the lane did not
    negotiate error-class-v2) never renders ``hop unknown``."""
    body = {"error": error}
    agent = type("A", (), {})()
    fbe.stash_api_error(agent, _Err(body, 504), 504)
    row = fbe.build_row(agent, "failover", from_provider="claude-dtlrf",
                        from_model="claude-fable-5-1", to_provider="claude-btpr",
                        to_model="claude-fable-5-1")
    assert row["hop"] == fp.normalize_hop(fbe.RELAY_SYNTHETIC_HOP[error])
    assert row["relay_error"] == error
    text = fp.format_cause_rider(row, tz=UTC)
    assert fp.HOP_UNKNOWN not in text, text


def test_relay_synthetic_classes_match_the_relay_contract():
    """Bodies the relay files as conn / pool_pressure / quota_model classify
    the same here (claude-pool ``_SYNTHETIC_CLASS``). The four the text table
    does not name (client cancelled, dispatch error, confirm probe, /v1/models)
    keep their hop; their class stays the text table's call."""
    relay_class = {
        "pool at capacity": "pool_pressure", "upstream connect timed out": "conn",
        "upstream attempt timed out": "conn", "pool deadline exceeded": "conn",
        "upstream unreachable on every box": "conn", "upstream unreachable": "conn",
        "upstream capacity unavailable for the requested model": "pool_pressure",
        "overflow_exhausted": "pool_pressure", "no eligible sub": "quota_model",
    }
    for error, cls in relay_class.items():
        assert error in fbe.RELAY_SYNTHETIC_HOP
        assert fbe.classify_text(error) == cls, error


def test_relay_hop_header_still_wins_over_the_body_table():
    body = {"error": "upstream attempt timed out"}
    agent = type("A", (), {})()
    fbe.stash_api_error(agent, _Err(body, 504, {"x-relay-error-hop": "relay"}), 504)
    row = fbe.build_row(agent, "failover", from_provider="claude-bpr", from_model="m",
                        to_provider="x", to_model="m")
    assert row["hop"] == "relay"


def test_unknown_body_still_renders_hop_unknown():
    """Negative control: a body that matches no relay-synthetic row."""
    agent = type("A", (), {})()
    fbe.stash_api_error(agent, _Err({"error": "something new"}, 502), 502)
    row = fbe.build_row(agent, "failover", from_provider="claude-dtlrf", from_model="m",
                        to_provider="x", to_model="m")
    assert not row.get("hop") and not row.get("relay_error")
    assert fp.HOP_UNKNOWN in fp.format_cause_rider(row, tz=UTC)


def test_relay_json_in_exception_text_is_recognised():
    assert fbe.relay_synthetic_error(
        None, 'HTTP 504: {"error":"upstream attempt timed out"}') == "upstream attempt timed out"
    assert fbe.relay_synthetic_error(None, "upstream attempt timed out") is None
    assert fbe.relay_synthetic_error({"error": {"message": "pool at capacity"}}) is None


def test_pool_eligible_header_parsing():
    assert fbe.pool_eligible({"x-pool-other-eligible": "0"}) == 0
    assert fbe.pool_eligible({"x-pool-other-eligible": "3"}) == 3
    assert fbe.pool_eligible({"x-relay-eligible": "0"}) == 0
    # v2 counts the failing seat too: only 0 proves there was no other.
    assert fbe.pool_eligible({"x-relay-eligible": "1"}) is None
    assert fbe.pool_eligible({"x-relay-eligible": "?"}) is None
    assert fbe.pool_eligible({}) is None


class _Resp:
    def __init__(self, headers):
        self.headers = headers

    def json(self):
        raise ValueError


class _Err(Exception):
    def __init__(self, body, status, headers=None):
        super().__init__(f"HTTP {status}: {body}")
        self.body = body
        self.status_code = status
        self.response = _Resp(headers or {})


# ── real failover path: the two live lines end to end ────────────────────

def test_live_2118_empty_reply_line(_home, monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    usage = type("U", (), {"output_tokens": 128})()
    resp = type("R", (), {"content": [], "stop_reason": "tool_use", "usage": usage,
                          "pool_headers": {
                              "x-pool-served-by": "sub-vps-23",
                              "x-pool-route-id": "054fb478cf644ad99659e051e75c5620",
                              "x-pool-empty-content-retried": "gave_up",
                              "x-pool-empty-content-attempts": "sub-vps-18,sub-vps-18,sub-vps-23",
                          }})()
    assert fbe.relay_gave_up_empty(resp)
    fbe.stash_response_failure(a, "invalid_response", resp, elapsed_s=25.28, repeat=True)
    assert try_activate_fallback(a) is True
    text = _rows(_home)[0]["notice_text"]
    assert text.startswith("🔄 Model fallback (empty tool-call reply ×3): "
                           "claude-alr/claude-fable-5-1 → claude-btpr/claude-fable-5-1"), text
    assert (" — empty tool-call reply ×3 from sub-vps-18 (stop_reason=tool_use, 0 content blocks,"
            " ~130 out) — relay retried ×3 across sub-vps-18, sub-vps-23, gave up, ") in text, text
    for banned in ("from Anthropic", "hop=relay-200", "hop unknown", "unclassified"):
        assert banned not in text, (banned, text)


def test_gave_up_line_and_row_carry_request_ids_and_prompt(_home, monkeypatch):
    """t_c706fd1e: the 10-05 14:09 ladder (sub-vps-8 x2 + sub-vps-15, ~342k
    prompt). The banner names each billed attempt's request id and the prompt
    size; the fallback_events row stores both plus the elapsed time."""
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    usage = type("U", (), {"output_tokens": 243, "input_tokens": 2,
                           "cache_read_input_tokens": 16008,
                           "cache_creation_input_tokens": 326348})()
    rids = "req_011CfjmrBqPSu3XPnAWyFRQW,req_011CfjmsQRi8Scuyg1ioPky7,req_011CfjmtY76qe92qW4bgDwDP"
    resp = type("R", (), {"content": [], "stop_reason": "tool_use", "usage": usage,
                          "pool_headers": {
                              "x-pool-served-by": "sub-vps-15",
                              "x-pool-route-id": "1aaf70a13df541858a26076c149a60a0",
                              "x-pool-empty-content-retried": "gave_up",
                              "x-pool-empty-content-attempts": "sub-vps-8,sub-vps-8,sub-vps-15",
                              "x-pool-empty-content-request-ids": rids,
                          }})()
    fbe.stash_response_failure(a, "invalid_response", resp, elapsed_s=47.4, repeat=True)
    assert try_activate_fallback(a) is True
    row = _rows(_home)[0]
    text = row["notice_text"]
    assert (" — empty tool-call reply ×3 from sub-vps-8 (stop_reason=tool_use, 0 content blocks,"
            " ~240 out); req_…") in text, text
    for rid in rids.split(","):
        assert f"req_…{rid[-6:]}" in text, (rid, text)
    assert " prompt 342k tok — relay retried ×3 across sub-vps-8, sub-vps-15, gave up" in text, text
    assert row["request_ids"] == rids
    assert row["prompt_tokens"] == 2 + 16008 + 326348
    assert row["elapsed_s"] == 47.4
    assert row["trigger_class"] == "provider_invalid_response"


def test_perturbation_header_is_captured_and_rendered(_home, monkeypatch):
    """t_9783560a: x-pool-empty-content-perturbations survives the response
    header snapshot, lands in floor.relay_perturbations, and names the rungs."""
    from agent.chat_completion_helpers import _snapshot_pool_headers, try_activate_fallback
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    wire = {"x-pool-served-by": "sub-vps-9",
            "x-pool-empty-content-retried": "gave_up",
            "x-pool-empty-content-attempts": "sub-vps-22,sub-vps-22,sub-vps-9",
            "x-pool-empty-content-perturbations": "none,drop_fgts,drop_fgts+rotate"}
    http = type("H", (), {"headers": wire})()
    pool_headers = _snapshot_pool_headers(http)
    assert pool_headers["x-pool-empty-content-perturbations"] == wire[
        "x-pool-empty-content-perturbations"]

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    usage = type("U", (), {"output_tokens": 243})()
    resp = type("R", (), {"content": [], "stop_reason": "tool_use", "usage": usage,
                          "pool_headers": pool_headers})()
    fbe.stash_response_failure(a, "invalid_response", resp, elapsed_s=30.0, repeat=True)
    assert a._pending_fallback_error["floor"]["relay_perturbations"] == [
        "none", "drop_fgts", "drop_fgts+rotate"]
    assert try_activate_fallback(a) is True
    text = _rows(_home)[0]["notice_text"]
    assert text.startswith("🔄 Model fallback (empty tool-call reply ×3): "), text
    assert (" — empty tool-call reply ×3 from sub-vps-22 (stop_reason=tool_use, 0 content blocks,"
            " ~240 out): as-is, −fgts beta, rotate→sub-vps-9 −fgts — relay ladder exhausted,"
            " falling to next lane, ") in text, text


def test_perturbation_parse_bounds():
    assert fbe._perturbations(None) is None
    assert fbe._perturbations("  ") is None
    assert fbe._perturbations(" none , drop_fgts ") == ["none", "drop_fgts"]
    assert fbe._perturbations(",".join(["none"] * 12)) == ["none"] * 8
    # One bad token rejects the whole list: dropping it would shift every later
    # rung onto the wrong attempt (Prism P1, PR #1821).
    assert fbe._perturbations("x" * 33 + ",rotate") is None
    assert fbe._perturbations("none,bad token!,drop_fgts+rotate") is None
    assert fbe._perturbations("none,,drop_fgts") is None


def test_live_2125_seat_timeout_line(_home, monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    a.provider = "claude-dtlrf"
    err = _Err({"error": "upstream attempt timed out"}, 504,
               {"x-pool-served-by": "sub-vps-18", "x-pool-other-eligible": "0"})
    fbe.stash_api_error(a, err, 504, elapsed_s=420.0)
    assert try_activate_fallback(a, reason=FailoverReason.pool_stalled) is True
    text = _rows(_home)[0]["notice_text"]
    assert text.startswith("🔄 Model fallback (seat timed out): claude-dtlrf/"), text
    assert (" — sub-vps-18 did not answer in 7m00s (relay deadline)"
            " · pool had no other seat, ") in text, text
    for banned in ("read timeout", "hop unknown", "stalled mid-turn"):
        assert banned not in text, (banned, text)
