"""t_5d79bfea: two fallback riders from 2026-10-07 21:52 (session
20261007_215000_ac99b02a) said things that were false.

* 21:52:26 alr -> dtlr: ``content policy refusal (hop unknown, sub unknown)``.
  The refusal was a billed HTTP 200 (``stop_reason=refusal``) served by
  sub-vps-11, which the relay named in ``x-pool-served-by``. The refusal path
  failed over with an empty evidence slot.
* 21:52:27 dtlr -> btpr: ``(bad request) ... unclassified error (Anthropic 409)
  on sub-vps-23``. The body was OUR bridge's ``tui_busy`` code: this session's
  previous turn (interrupted client-side 14 s earlier) was still running there.

Both lines are pinned through the real failover + renderer, with negatives.
Every bridge machine code (claude-bpx ``TUI_ERRORS``) is pinned to a named
class and a bridge hop through ONE table.
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)
import httpx
import openai
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.agent_runtime_helpers import extract_api_error_context
from agent.error_classifier import FailoverReason, classify_api_error
from agent.fallback_capability import BRIDGE_ERROR_CODES, bridge_error_code
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401

UTC = dt.timezone.utc
TS = dt.datetime(2026, 10, 8, 4, 52, 26, tzinfo=UTC).timestamp()

# claude-bpx bridge/src/tuiRunner.js TUI_ERRORS at 3b7ab72 (code -> HTTP status).
# Pinned here so a new bridge code is a red test, not an "unclassified" banner.
BRIDGE_TUI_ERRORS = {
    "mode_not_allowed": 400, "tui_tools_unsupported": 400, "tui_images_unsupported": 400,
    "tui_no_session_key": 400, "tui_last_not_user": 400, "tui_turn_too_large": 400,
    "tui_busy": 409, "tui_history_diverged": 409, "tui_capacity": 503,
    "seat_capacity": 503, "tui_state_unwritable": 503, "tui_startup": 503,
    "tui_turn_timeout": 504, "tui_upstream_error": 502, "tui_rate_limited": 429,
    "safeguard_refusal": 400, "entrypoint_mismatch": 502, "tui_tool_unknown": 400,
    "tui_tool_duplicate": 400, "tui_tool_result_too_large": 400,
    "tui_ambiguous_parallel": 502, "tui_mcp_not_ready": 503, "tui_tool_not_host": 502,
    "tui_tool_mismatch": 502, "tui_tool_uncorrelated": 502, "tui_tools_invalid": 400,
    "tui_cancelled": 409, "context_length_exceeded": 400, "tui_config": 500,
}

BUSY_MSG = "a different turn is already in flight for this session; retry after it completes"
BUSY_HEADERS = {"x-relay-error-class": "upstream_passthrough",
                "x-relay-error-hop": "bridge->upstream",
                "x-relay-seat": "sub-vps-23", "x-relay-eligible": "23"}


def _bridge_err(code: str, status: int, message: str = "bridge said no",
                headers: dict | None = None) -> openai.APIStatusError:
    data = {"error": {"type": "invalid_request_error", "code": code, "message": message}}
    req = httpx.Request("POST", "http://127.0.0.1:18816/v1/chat/completions")
    resp = httpx.Response(status, json=data, request=req, headers=headers or {})
    # OpenAI SDK raises with body=data["error"] (the inner object).
    return openai.APIStatusError(f"Error code: {status} - {data}", response=resp,
                                 body=data["error"])


def _row_for(err, status, *, from_provider="claude-dtlr", reason=None):
    agent = SimpleNamespace(session_id="s1", _current_turn_id="s1:1")
    fbe.stash_api_error(agent, err, status, extract_api_error_context(err))
    row = fbe.build_row(agent, "failover", from_provider=from_provider,
                        from_model="claude-fable-5-1", to_provider="claude-btpr",
                        to_model="claude-fable-5-1", reason=reason)
    row["ts"] = TS
    return row


def _refusal_resp(**pool):
    headers = {"x-pool-served-by": "sub-vps-11",
               "x-pool-route-id": "1ed441e2fc3f419b8ef8bc09a88fc793"}
    headers.update(pool)
    return SimpleNamespace(pool_headers={k: v for k, v in headers.items() if v is not None},
                           usage=SimpleNamespace(output_tokens=0), stop_reason="refusal",
                           content=[])


def _refusal_row(resp, stop_details=None, from_provider="claude-alr"):
    agent = SimpleNamespace(session_id="s1", _current_turn_id="s1:1",
                            provider=from_provider, model="claude-opus-5-5")
    fbe.stash_refusal(agent, resp, stop_details=stop_details)
    row = fbe.build_row(agent, "failover", from_provider=from_provider,
                        from_model="claude-opus-5-5", to_provider="claude-dtlr",
                        to_model="claude-fable-5-1", reason="content_policy_blocked")
    row["ts"] = TS
    return row


# ── A. HTTP-200 refusal ──────────────────────────────────────────────────

def test_pooled_refusal_names_hop_and_seat():
    row = _refusal_row(_refusal_resp(), {"category": "cyber"})
    assert row["trigger_class"] == "refusal"
    assert row["seat"] == "sub-vps-11"
    assert row["route_id"] == "1ed441e2fc3f419b8ef8bc09a88fc793"
    assert row["http_status"] == 200
    text, floors = fp.cause_rider_with_floors(row, tz=UTC)
    assert text == ("content policy refusal (category=cyber) · hop=relay-200 · sub=sub-vps-11, "
                    "04:52:26"), text
    assert floors == ()
    assert "hop unknown" not in text and "sub unknown" not in text


def test_refusal_without_category_and_with_explanation():
    expl = "This request asks for operational help " + "x" * 120
    row = _refusal_row(_refusal_resp(), {"explanation": expl})
    text = fp.format_cause_rider(row, tz=UTC)
    assert text.startswith("content policy refusal · hop=relay-200 · sub=sub-vps-11 · \"")
    match = re.search(r'· "([^"]*)"', text)
    assert match, text
    quoted = match.group(1)
    assert len(quoted) <= fbe.REFUSAL_EXPLANATION_MAX and quoted.endswith("…")
    assert "(category=" not in text


def test_refusal_seat_hidden_when_seat_names_off():
    row = _refusal_row(_refusal_resp(), {"category": "cyber"})
    assert "sub=a sub" in fp.format_cause_rider(row, seat_names=False, tz=UTC)


def test_refusal_with_floor_never_reaches_the_dead_letter_sentinel(tmp_path):
    row = _refusal_row(_refusal_resp(), {"category": "cyber"})
    text, floors = fp.cause_rider_with_floors(row, tz=UTC)
    ledger = tmp_path / "dead.jsonl"
    assert fbe.note_unclassified(row, text, floors, path=ledger) is False
    assert not ledger.exists()


def test_refusal_from_a_plain_provider_keeps_the_plain_shape():
    # Positive control: a direct, non-pooled provider has no hop/sub vocabulary.
    resp = SimpleNamespace(pool_headers=None, usage=SimpleNamespace(output_tokens=0),
                           stop_reason="refusal", content=[])
    row = _refusal_row(resp, {"category": "cyber"}, from_provider="anthropic")
    text = fp.format_cause_rider(row, tz=UTC)
    assert text == "content policy refusal, 04:52:26", text


def test_refusal_without_stash_still_renders_the_old_floor():
    # Negative control for the stash: without evidence the floor still says so.
    agent = SimpleNamespace(session_id="s1", _current_turn_id="s1:1")
    row = fbe.build_row(agent, "failover", from_provider="claude-alr",
                        from_model="claude-opus-5-5", to_provider="claude-dtlr",
                        to_model="claude-fable-5-1", reason="content_policy_blocked")
    row["ts"] = TS
    text, floors = fp.cause_rider_with_floors(row, tz=UTC)
    assert "(hop unknown, sub unknown)" in text and fp.FLOOR_HOP_SUB in floors


def test_refusal_floor_has_no_err_hash_so_same_error_backoff_is_unchanged():
    agent = SimpleNamespace()
    fbe.stash_refusal(agent, _refusal_resp(), stop_details={"category": "cyber"})
    assert fbe.pending_err_hash(agent._pending_fallback_error) is None


def test_record_refusal_writes_one_census_line(tmp_path):
    agent = SimpleNamespace(provider="claude-alr", model="claude-opus-5-5", session_id="s1")
    fbe.stash_refusal(agent, _refusal_resp(), stop_details={"category": "cyber"})
    path = tmp_path / "model-route-changes.log"
    assert fbe.record_refusal(agent, agent._pending_fallback_error["floor"], path=path)
    line = path.read_text().strip()
    assert re.match(r"^\d{4}-\d\d-\d\dT[\d:]{8} refusal class=refusal provider=claude-alr "
                    r"model=claude-opus-5-5 served_by=sub-vps-11 "
                    r"route_id=1ed441e2fc3f419b8ef8bc09a88fc793 category=cyber "
                    r"stop_reason=refusal output_tokens=0 session=s1$", line), line


def test_live_215226_refusal_line_through_the_real_handler(_home, monkeypatch):
    """The refusal handler stashes before it fails over, so the real announce
    names the seat. Mutation: drop the stash_refusal call -> RED."""
    from agent.turn_retry_state import TurnRetryState
    from agent.turn_truncation import handle_content_policy_refusal
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    a.model = "claude-opus-5-5"
    resp = _refusal_resp()
    msg = SimpleNamespace(content="", provider_data={"stop_details": {"category": "cyber"}})
    monkeypatch.setattr("agent.turn_truncation.normalize_response_for_agent",
                        lambda _agent, _resp: msg)
    monkeypatch.setattr("agent.conversation_loop._arm_fallback_restart",
                        lambda _a, _m, sp, _r: sp)
    a._extract_reasoning = lambda _m: ""
    a._invoke_api_request_error_hook = lambda **_kw: None
    a.thinking_callback = None
    from agent.chat_completion_helpers import try_activate_fallback

    a._has_pending_fallback = lambda: True
    a._buffer_diagnostic_status = lambda *_: None
    a._try_activate_fallback = lambda **kw: try_activate_fallback(a, **kw)
    verdict = handle_content_policy_refusal(
        a, resp, TurnRetryState(), thinking_spinner=None, messages=[], api_messages=[],
        api_kwargs=None, active_system_prompt="sp", conversation_history=[],
        api_call_count=1, effective_task_id="t", turn_id="t1", api_request_id="r",
        api_start_time=0.0, retry_count=0, max_retries=3)
    assert verdict.action == "break"
    text = _rows(_home)[0]["notice_text"]
    assert text.startswith("🔄 Model fallback (safety refusal): claude-alr/claude-opus-5-5 → "), text
    assert " — content policy refusal (category=cyber) · hop=relay-200 · sub=sub-vps-11, " in text
    for banned in ("hop unknown", "sub unknown", "unclassified"):
        assert banned not in text, (banned, text)
    log = (_home / "state" / "model-route-changes.log").read_text()
    assert " refusal class=refusal provider=claude-alr " in log
    assert "served_by=sub-vps-11" in log and "category=cyber" in log


# ── B. bridge 409 tui_busy ───────────────────────────────────────────────

def test_tui_busy_classifies_by_code_not_status():
    err = _bridge_err("tui_busy", 409, BUSY_MSG, BUSY_HEADERS)
    c = classify_api_error(err, provider="claude-dtlr", model="claude-fable-5-1")
    assert c.reason is FailoverReason.session_busy
    assert c.should_fallback and not c.should_rotate_credential
    # Text alone (no code) is not enough: the code is the contract.
    plain = _bridge_err("something_else", 409, BUSY_MSG)
    assert classify_api_error(plain).reason is not FailoverReason.session_busy


def test_tui_busy_rider_names_the_bridge():
    row = _row_for(_bridge_err("tui_busy", 409, BUSY_MSG, BUSY_HEADERS), 409,
                   reason=FailoverReason.session_busy)
    assert row["trigger_class"] == "pool_pressure"
    assert row["hop"] == "relay→bridge"
    assert row["seat"] == "sub-vps-23"
    assert fp.head_label_override(row) == "session busy"
    text, floors = fp.cause_rider_with_floors(row, tz=UTC)
    assert text == ("interactive session busy — 409 tui_busy at the bridge (ours, not Anthropic) "
                    "on sub-vps-23; this session's previous turn is still running, 04:52:26"), text
    assert floors == ()
    for banned in ("bad request", "unclassified", "Anthropic 409", fp.POOL_NO_OTHER_SEAT):
        assert banned not in text, (banned, text)


def test_tui_busy_with_no_other_seat_says_so():
    hdrs = dict(BUSY_HEADERS, **{"x-relay-eligible": "0"})
    row = _row_for(_bridge_err("tui_busy", 409, BUSY_MSG, hdrs), 409)
    assert fp.format_cause_rider(row, tz=UTC).endswith(
        f"still running · {fp.POOL_NO_OTHER_SEAT}, 04:52:26")


def test_live_215227_busy_line_through_the_real_failover(_home, monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    a.provider = "claude-dtlr"
    err = _bridge_err("tui_busy", 409, BUSY_MSG, BUSY_HEADERS)
    fbe.stash_api_error(a, err, 409, extract_api_error_context(err))
    assert try_activate_fallback(a, reason=FailoverReason.session_busy) is True
    text = _rows(_home)[0]["notice_text"]
    assert text.startswith("🔄 Model fallback (session busy): claude-dtlr/claude-fable-5-1 → "
                           "claude-btpr/claude-fable-5-1"), text
    assert (" — interactive session busy — 409 tui_busy at the bridge (ours, not Anthropic) "
            "on sub-vps-23; this session's previous turn is still running, ") in text, text
    for banned in ("bad request", "unclassified", "Anthropic 409", fp.POOL_NO_OTHER_SEAT):
        assert banned not in text, (banned, text)


def test_tui_busy_waits_once_then_falls_over(monkeypatch):
    """One bounded same-route wait without consuming an attempt; a second busy
    activates the fallback. Mutation: drop the session_busy branch -> RED."""
    import agent.turn_recovery as tr
    from agent.turn_retry_state import TurnRetryState

    monkeypatch.setattr(tr, "SESSION_BUSY_WAIT_S", 0.0)
    calls = []
    agent = SimpleNamespace(
        _interrupt_requested=False, _fallback_index=0,
        _fallback_chain=[{"provider": "claude-btpr", "model": "claude-fable-5-1"}],
        _touch_activity=lambda *_: None, _client_log_context=lambda: "",
        _buffer_diagnostic_status=lambda *_: None, provider="claude-dtlr",
        _try_activate_fallback=lambda **kw: calls.append(kw) or True,
        _capacity_retry_attempts=0, compression_enabled=True, tools=None,
    )
    monkeypatch.setattr("agent.conversation_loop._arm_fallback_restart",
                        lambda _a, _m, sp, _r: sp)
    err = _bridge_err("tui_busy", 409, BUSY_MSG, BUSY_HEADERS)
    classified = classify_api_error(err, provider="claude-dtlr")
    retry = TurnRetryState()

    def _route(retry_count):
        return tr.route_classified_error(
            agent, err, classified, retry, error_msg=str(err), error_context={},
            recovered_with_pool=False, base_url="http://127.0.0.1:18816/v1", model="m",
            messages=[], api_messages=[], system_message="", active_system_prompt="sp",
            conversation_history=[], retry_count=retry_count, max_retries=3,
            compression_attempts=0, max_compression_attempts=3, api_call_count=1,
            effective_task_id="t")

    first = _route(1)
    assert first.action == "continue" and first.retry_count == 0 and not calls
    second = _route(1)
    assert second.action == "break"
    assert calls and calls[0]["reason"] is FailoverReason.session_busy


# ── every bridge code: named class, bridge hop, never "Anthropic" ────────

def test_table_covers_exactly_the_bridge_codes():
    assert set(BRIDGE_ERROR_CODES) == set(BRIDGE_TUI_ERRORS)


@pytest.mark.parametrize("code,status", sorted(BRIDGE_TUI_ERRORS.items()))
def test_every_bridge_code_is_named_and_ours(code, status):
    hdrs = {"x-relay-error-class": "upstream_passthrough",
            "x-relay-error-hop": "bridge->upstream", "x-relay-seat": "sub-vps-23"}
    err = _bridge_err(code, status, "bridge message", hdrs)
    row = _row_for(err, status)
    assert bridge_error_code({"error": {"code": code}}) == code
    assert row["trigger_class"] == BRIDGE_ERROR_CODES[code].trigger_class
    assert row["trigger_class"] != "unclassified"
    assert row["hop"] == ("relay" if code == "mode_not_allowed" else "relay→bridge")
    text, floors = fp.cause_rider_with_floors(row, tz=UTC)
    assert "Anthropic" not in text.replace("ours, not Anthropic", ""), text
    assert "unclassified" not in text and fp.FLOOR_CAUSE not in floors, text
    assert fp.HOP_UNKNOWN not in text, text
    assert f"{status} {code} at the " in text, text
    head = fp.head_label_override(row)
    assert head != "bad request"
