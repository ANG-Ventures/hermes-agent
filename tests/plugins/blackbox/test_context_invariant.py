"""Relay usage invariant (claude-bpx#397, card t_5918f6f7).

A relay turn that re-sent its prompt (safeguard auto-continue) reported the
SUM of two upstream prompts as one prompt: 1,795,696 on a 1M window. Both
requests were billed, so the billing columns stay exactly as reported; only
CONTEXT SIZE readers switch to the corrected figure.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from plugins import blackbox
from plugins.blackbox import store
from plugins.blackbox.last_turn import render_last_turn_record
from plugins.blackbox.record import effective_context_used

_LEGACY_TURNS_COLS = (
    "turn_id TEXT PRIMARY KEY, parent_turn_id TEXT, is_subagent INT,"
    " ts_start REAL, ts_end REAL, profile TEXT, provider TEXT, model TEXT,"
    " platform TEXT, chat_id TEXT, chat_name TEXT, api_calls INT, tools TEXT,"
    " input_tokens INT, output_tokens INT, cache_read INT, cache_write INT,"
    " reasoning INT, context_used INT, context_length INT, cost_usd REAL,"
    " cost_status TEXT, interrupted INT, alerted INT DEFAULT 0,"
    " user_text TEXT, final_text TEXT"
)


@pytest.fixture(params=["fresh", "legacy"])
def db(request, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if request.param == "legacy":
        path = store._db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = sqlite3.connect(str(path))
        legacy.execute(f"CREATE TABLE turns ({_LEGACY_TURNS_COLS})")
        legacy.commit()
        legacy.close()
    store._connect().close()
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650,
    })
    return store._db_path()


def _bridge_usage(prompt, **relay):
    """OpenAI-shaped bpx/bpr egress usage, plus #397's relay fields."""
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=120, total_tokens=prompt + 120,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        **relay,
    )


def _run_turn(turn_id, usage, *, context_used, context_length=1_000_000):
    session = f"s-{turn_id}"
    blackbox._on_session_start(session_id=session)
    blackbox.record_api_call(
        turn_id=turn_id, seq=0, ts=100.0, provider="claude-bpr",
        model="claude-opus-5-5", usage=usage, api_mode="chat_completions",
        sub_key=None, attribution="inferred", http_status=200,
        relay_synthetic=False, route_id=None,
    )
    blackbox._on_session_end(
        session_id=session, turn_id=turn_id, provider="claude-bpr",
        model="claude-opus-5-5", platform="cli",
        turn_usage={"input_tokens": context_used, "output_tokens": 120,
                    "api_calls": 1, "context_used": context_used,
                    "context_length": context_length},
    )


def _row(db, sql, *args):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchone()


def test_flagged_relay_row_keeps_billing_and_corrects_context(db):
    flagged = _bridge_usage(
        1_795_696, usage_invariant_violation="prompt_exceeds_context_window",
        relay_synthetic=True, context_window=1_000_000, upstream_requests=2,
        cumulative_prompt_tokens=1_795_696,
    )
    _run_turn("t-flag", flagged, context_used=1_795_696)
    _run_turn("t-ctl", _bridge_usage(1_795_696), context_used=1_795_696)

    call = _row(db, "SELECT * FROM turn_api_calls WHERE turn_id='t-flag'")
    ctl = _row(db, "SELECT * FROM turn_api_calls WHERE turn_id='t-ctl'")
    assert call["usage_invariant_violation"] == "prompt_exceeds_context_window"
    assert call["corrected_prompt_tokens"] == 1_000_000
    assert call["upstream_requests"] == 2
    # relay_synthetic keeps its own class (pool-unreachable synthetic errors).
    assert call["relay_synthetic"] == 0
    # Billing is untouched: the flagged row stores exactly what the control does.
    for col in ("input_tokens", "cache_read", "cache_write", "output_tokens"):
        assert call[col] == ctl[col]
    assert ctl["usage_invariant_violation"] is None
    assert ctl["corrected_prompt_tokens"] is None

    turn = _row(db, "SELECT * FROM turns WHERE turn_id='t-flag'")
    ctl_turn = _row(db, "SELECT * FROM turns WHERE turn_id='t-ctl'")
    assert turn["context_used"] == 1_795_696  # raw kept
    assert turn["corrected_context_used"] == 1_000_000
    assert turn["cost_usd"] == ctl_turn["cost_usd"]
    assert turn["input_tokens"] == ctl_turn["input_tokens"]


def test_in_window_turn_is_not_corrected(db):
    _run_turn("t-ok", _bridge_usage(400_000, upstream_requests=1), context_used=400_000)
    turn = _row(db, "SELECT corrected_context_used FROM turns WHERE turn_id='t-ok'")
    call = _row(db, "SELECT * FROM turn_api_calls WHERE turn_id='t-ok'")
    assert turn["corrected_context_used"] is None
    assert call["usage_invariant_violation"] is None
    assert call["upstream_requests"] == 1


def test_unflagged_over_length_turn_is_left_alone(db):
    """context_used > context_length without the relay flag is not corrected:
    context_length can be the harness's configured length (272k on codex
    turns with real 300k+ prompts), not the model window."""
    _run_turn("t-old", _bridge_usage(325_800), context_used=325_800, context_length=272_000)
    turn = _row(db, "SELECT corrected_context_used FROM turns WHERE turn_id='t-old'")
    assert turn["corrected_context_used"] is None


def test_backfilled_last_request_figure_survives_refinalize(db):
    """A backfilled call correction (last-request prompt from the wire) wins
    over the window, and a turn re-finalize does not erase it."""
    _run_turn("t-bf", _bridge_usage(1_795_696), context_used=1_795_696)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE turn_api_calls SET usage_invariant_violation='prompt_exceeds_context_window',"
            " corrected_prompt_tokens=897883 WHERE turn_id='t-bf'")
        store._refresh_context_invariant(conn, "t-bf")
    assert _row(db, "SELECT corrected_context_used c FROM turns WHERE turn_id='t-bf'")["c"] == 897_883
    blackbox._on_session_start(session_id="s-t-bf")
    blackbox._on_session_end(
        session_id="s-t-bf", turn_id="t-bf", provider="claude-bpr",
        model="claude-opus-5-5", platform="cli",
        turn_usage={"input_tokens": 1_795_696, "output_tokens": 120, "api_calls": 1,
                    "context_used": 1_795_696, "context_length": 1_000_000},
    )
    assert _row(db, "SELECT corrected_context_used c FROM turns WHERE turn_id='t-bf'")["c"] == 897_883


def test_auxiliary_call_does_not_clear_main_context_correction(db):
    flagged = _bridge_usage(
        1_795_696, usage_invariant_violation="prompt_exceeds_context_window",
        relay_synthetic=True, context_window=1_000_000, upstream_requests=2,
    )
    _run_turn("t-aux", flagged, context_used=1_795_696)
    # A title/compression call can land AFTER the main call and has a higher seq;
    # its prompt is not the turn's context size (Prism round 1 on #1563).
    blackbox.record_api_call(
        turn_id="t-aux", seq=1, ts=101., provider="gemini-bridge",
        model="gemini-3-flash", usage=_bridge_usage(1234), api_mode="chat_completions",
        sub_key=None, attribution="aux:title_generation", http_status=200,
        relay_synthetic=False, route_id=None,
    )
    assert _row(db, "SELECT corrected_context_used c FROM turns WHERE turn_id='t-aux'")["c"] == 1_000_000


def test_card_hydration_and_just_finalized_record_receive_correction(db, monkeypatch):
    from plugins.blackbox import card
    from plugins.blackbox.record import TurnRecord

    flagged = _bridge_usage(
        1_795_696, usage_invariant_violation="prompt_exceeds_context_window",
        relay_synthetic=True, context_window=1_000_000, upstream_requests=2,
    )
    _run_turn("t-card", flagged, context_used=1_795_696)
    row = dict(_row(db, "SELECT * FROM turns WHERE turn_id='t-card'"))
    rec = card._record_from_row(row)
    assert rec.corrected_context_used == 1_000_000
    assert "100%" in card._context_line(rec)
    assert "180%" not in card._context_line(rec)
    # Proactive cards render the freshly built record (not a rehydration).
    rec2 = TurnRecord(turn_id="t-card", context_used=1_795_696, context_length=1_000_000)
    store.insert_turn(rec2)
    assert rec2.corrected_context_used == 1_000_000
    assert "180%" not in card._context_line(rec2)


def test_context_line_renders_corrected_and_names_raw():
    rec = {"context_used": 1_795_696, "context_length": 1_000_000,
           "corrected_context_used": 897_883, "input_tokens": 1_795_696,
           "output_tokens": 120, "last_call_prompt_unknown": 0}
    lines = render_last_turn_record(rec)
    ctx = [ln for ln in lines if ln.startswith("• Context window")]
    assert ctx and "1.8M" not in ctx[0] and "90%" in ctx[0], lines
    assert any("exceeds the window" in ln and "1.8M" in ln for ln in lines), lines


def test_effective_context_used_shapes():
    assert effective_context_used({"context_used": 5, "context_length": 10}) == (5, None)
    assert effective_context_used({"context_used": 15, "context_length": 10}) == (15, None)
    assert effective_context_used(SimpleNamespace(context_used=15, corrected_context_used=10)) == (10, 15)
    assert effective_context_used(
        {"context_used": 15, "context_length": 10, "corrected_context_used": 7}) == (7, 15)
