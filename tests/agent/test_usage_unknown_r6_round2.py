"""r6 FleetReview round-2 regressions — findings 3, 9 and 12.

The r6 commit disposed 9 of the record's 13 findings; these are the three that
were left undisposed and are fixed here. Each test drives real code (the real
turn rollup, the shipped renderer, a real store round-trip through the
production row shape) and carries a narrowness control so the fix cannot be
bought by making the UNKNOWN refusal unreachable.

  F9  [P2] a turn-level absorbing `input_unknown` blanked three rows that
           describe the FINAL call only, so one usage-less call anywhere in a
           multi-call turn discarded a fully measured final call's real
           window/cache numbers.
  F3  [P3] `cache_stats_line` spoke on every usage-less call, where the inline
           code it replaced printed nothing — one noise line per API call.
  F12 [P3] the store round-trip tests fed the renderer `get_turn`'s renamed
           row shape, which production never produces.
"""
import sqlite3

import pytest

from agent.usage_pricing import (
    CanonicalUsage,
    cache_stats_line,
    normalize_usage,
)


def _call(**over):
    base = dict(
        input_tokens=0, output_tokens=0, cache_read_tokens=0,
        cache_write_tokens=0, reasoning_tokens=0, total_tokens=0,
    )
    base.update(over)
    return base


MEASURED_CALL = _call(
    input_tokens=100, output_tokens=50, cache_read_tokens=3900, total_tokens=4050
)
USAGELESS_CALL = _call(
    input_tokens_unknown=True, output_tokens_unknown=True,
    cache_read_tokens_unknown=True, cache_write_tokens_unknown=True,
    usage_unknown=True,
)


def _render(rec):
    from plugins.blackbox.last_turn import render_last_turn_record

    return "\n".join(render_last_turn_record(rec))


def _row_from_calls(calls):
    """A stored-turn-shaped dict built by the REAL turn rollup + last-call split.

    Mirrors agent/turn_finalizer.py: the turn totals come from
    ``_rollup_turn_usage`` (absorbing across every call) while the last-call
    split and its discriminator come from ``_turn_calls[-1]``.
    """
    from agent.turn_finalizer import _rollup_turn_usage

    roll = _rollup_turn_usage(calls)
    last = calls[-1]
    last_unknown = any(
        bool(last.get(k))
        for k in ("input_tokens_unknown", "cache_read_tokens_unknown",
                  "cache_write_tokens_unknown", "usage_unknown")
    )
    used = (
        int(last.get("input_tokens", 0) or 0)
        + int(last.get("cache_read_tokens", 0) or 0)
        + int(last.get("cache_write_tokens", 0) or 0)
    )
    return {
        **roll,
        "cache_read": roll["cache_read_tokens"],
        "cache_write": roll["cache_write_tokens"],
        "reasoning": roll["reasoning_tokens"],
        "cost_usd": None,
        "cost_status": "unknown",
        "context_used": used,
        "context_length": 200_000,
        "last_cache_read": int(last.get("cache_read_tokens", 0) or 0),
        "last_cache_write": int(last.get("cache_write_tokens", 0) or 0),
        "last_uncached": int(last.get("input_tokens", 0) or 0),
        "last_call_prompt_unknown": int(last_unknown),
        "tools": "[]",
    }


# --------------------------------------------------------------------------
# F9 — last-call rows must read the LAST CALL's provenance, not the turn's OR
# --------------------------------------------------------------------------


def test_f9_a_measured_final_call_keeps_its_window_numbers():
    """One usage-less call mid-turn must not blank the final call's real data."""
    rec = _row_from_calls([MEASURED_CALL, USAGELESS_CALL, MEASURED_CALL])
    # The turn-level flag IS set — that part is correct and unchanged.
    assert rec["input_tokens_unknown"] is True
    block = _render(rec)
    assert "Last call: 4k billed" in block, (
        "the final call was fully measured; its split is real data and must "
        "not be discarded because an EARLIER call of the turn went unmeasured"
    )
    assert "Context window (last call): unknown" not in block
    assert "Context window (last call): 4k/200k" in block


def test_f9_narrowness_an_unmeasured_final_call_is_still_unknown():
    """The refusal must stay reachable where it is actually true."""
    rec = _row_from_calls([MEASURED_CALL, USAGELESS_CALL])
    block = _render(rec)
    assert "Context window (last call): unknown" in block
    assert "Last call:" not in block


def test_f9_the_turn_level_cached_row_still_gates_on_the_turn_level_flag():
    """`• Cached:` sums the WHOLE turn, so the absorbing flag is right there."""
    rec = _row_from_calls([MEASURED_CALL, USAGELESS_CALL, MEASURED_CALL])
    block = _render(rec)
    assert "• Cached:" not in block, (
        "the hit-rate row divides turn-level sums that really are missing a "
        "term — computing a percentage over them would be a fabrication"
    )
    all_measured = _render(_row_from_calls([MEASURED_CALL, MEASURED_CALL]))
    assert "• Cached:" in all_measured


def test_f9_a_legacy_row_without_the_column_keeps_todays_behaviour():
    """NULL means 'provenance never recorded' → fall back to the turn flag."""
    rec = _row_from_calls([MEASURED_CALL, USAGELESS_CALL, MEASURED_CALL])
    rec["last_call_prompt_unknown"] = None
    block = _render(rec)
    assert "Context window (last call): unknown" in block
    assert "Last call:" not in block


def test_f9_the_flag_round_trips_through_the_real_store(tmp_path, monkeypatch):
    """Producer → store → production row shape → renderer, end to end."""
    import plugins.blackbox as bb
    import plugins.blackbox.store as store

    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "turns.db")
    calls = [MEASURED_CALL, USAGELESS_CALL, MEASURED_CALL]
    rec = bb._build_record(
        session_id="s", interrupted=False, model="claude-sonnet-4-5",
        platform="cli", provider="anthropic", user_message="", final_response="",
        turn_usage={
            **_row_from_calls(calls),
            "calls": calls,
            "last_cache_read_tokens": MEASURED_CALL["cache_read_tokens"],
            "last_cache_write_tokens": MEASURED_CALL["cache_write_tokens"],
            "last_uncached_tokens": MEASURED_CALL["input_tokens"],
            "last_call_prompt_unknown": False,
        },
        cfg={"store_text": False}, kwargs={},
    )
    assert rec.last_call_prompt_unknown is False
    store.insert_turn(rec)
    conn = sqlite3.connect(store._db_path())
    try:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (rec.turn_id,)
        ).fetchone())
    finally:
        conn.close()
    assert row["last_call_prompt_unknown"] == 0
    assert "Context window (last call): unknown" not in _render(row)


class _FinalizerAgent:
    """The minimum agent surface `finalize_turn` touches (cf. the MoA seam test)."""

    def __init__(self):
        from types import SimpleNamespace

        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "claude-sonnet-4-5"
        self.provider = "anthropic"
        self.base_url = ""
        self.session_id = "f9-producer"
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=4000, context_length=200_000
        )
        for _attr in (
            "session_input_tokens", "session_output_tokens",
            "session_cache_read_tokens", "session_cache_write_tokens",
            "session_reasoning_tokens", "session_prompt_tokens",
            "session_completion_tokens", "session_total_tokens",
            "session_estimated_cost_usd",
        ):
            setattr(self, _attr, 0)
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []

    def _handle_max_iterations(self, *_a):
        raise AssertionError("not expected")

    def __getattr__(self, name):  # no-op for the finalizer's optional callbacks
        if name.startswith("_") or name in {"clear_interrupt"}:
            return lambda *a, **k: None
        raise AttributeError(name)


def _producer_turn_usage(calls, monkeypatch):
    """Drive the REAL `finalize_turn` and capture the turn_usage it emits."""
    from agent.turn_finalizer import finalize_turn

    captured = {}

    def invoke_hook(name, **kwargs):
        if name == "on_session_end":
            captured["turn_usage"] = kwargs.get("turn_usage")
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)
    finalize_turn(
        _FinalizerAgent(),
        final_response="ok",
        api_call_count=len(calls),
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "hi"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
        _turn_calls=list(calls),
    )
    assert captured.get("turn_usage") is not None, "on_session_end carried no turn_usage"
    return captured["turn_usage"]


def test_f9_producer_reads_the_final_call_not_the_turn_aggregate(monkeypatch):
    """The flag the renderer trusts must come from `_turn_calls[-1]`.

    Pins the PRODUCER half by running the shipped `finalize_turn`, not a
    re-implementation of its fold: the renderer tests above build the row by
    hand, so a producer that hardcoded the flag would leave them all green.
    """
    measured_last = _producer_turn_usage(
        [MEASURED_CALL, USAGELESS_CALL, MEASURED_CALL], monkeypatch
    )
    # The absorbing turn-level flag is set — and is NOT the answer for the
    # final-call figures sitting beside it in the same payload.
    assert measured_last["input_tokens_unknown"] is True
    assert measured_last["last_call_prompt_unknown"] is False


def test_f9_producer_narrowness_an_unmeasured_final_call_still_reports_unknown(
    monkeypatch,
):
    unmeasured_last = _producer_turn_usage(
        [MEASURED_CALL, USAGELESS_CALL], monkeypatch
    )
    assert unmeasured_last["last_call_prompt_unknown"] is True
    all_measured = _producer_turn_usage([MEASURED_CALL, MEASURED_CALL], monkeypatch)
    assert all_measured["last_call_prompt_unknown"] is False


def test_f9_migration_of_a_preexisting_table_leaves_the_flag_null(tmp_path, monkeypatch):
    """A DEFAULT 0 would assert 'final call measured' about every old row."""
    import plugins.blackbox.store as store

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = store._db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    # Full pre-flag schema (mirrors tests/plugins/blackbox/test_store.py's
    # legacy fixture: everything a real old DB had, minus the new column).
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY, parent_turn_id TEXT, is_subagent INT,
            ts_start REAL, ts_end REAL, profile TEXT, provider TEXT, model TEXT,
            platform TEXT, chat_id TEXT, chat_name TEXT, api_calls INT, tools TEXT,
            input_tokens INT, output_tokens INT, cache_read INT, cache_write INT,
            reasoning INT, context_used INT, context_length INT, cost_usd REAL,
            cost_status TEXT, interrupted INT, alerted INT DEFAULT 0,
            user_text TEXT, final_text TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO turns (turn_id, input_tokens, output_tokens, cost_usd) "
        "VALUES ('turn_legacy', 100, 50, NULL)"
    )
    conn.commit()
    conn.close()

    with store._connect() as c:
        c.execute("SELECT 1").fetchone()

    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
        assert "last_call_prompt_unknown" in cols
        value = conn.execute(
            "SELECT last_call_prompt_unknown FROM turns WHERE turn_id='turn_legacy'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value is None, (
        "a historical row's final-call provenance was never recorded; "
        "claiming 0 (measured) would render its placeholder zeros as real"
    )


# --------------------------------------------------------------------------
# F3 — the console cache line must not speak where it used to stay silent
# --------------------------------------------------------------------------


def test_f3_a_usageless_call_prints_no_cache_line():
    usage = CanonicalUsage.fully_unknown()
    assert cache_stats_line(usage, usage.prompt_tokens) is None, (
        "this runs per API CALL; the inline code it replaced printed nothing "
        "when there was no cache activity, so a 12-call tool loop against a "
        "provider that omits usage would emit 12 noise lines"
    )


def test_f3_narrowness_a_null_cache_bucket_is_still_reported_unknown():
    """A PRESENT payload whose cache count is null IS cache-specific news.

    Only the AGGREGATE `usage_unknown` (no payload at all) silences the line;
    a narrower unknown still prints, because the percentage that would
    otherwise render is the fabrication this branch exists to refuse.
    """
    usage = normalize_usage({
        "prompt_tokens": 150, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": None},
    })
    assert usage.usage_unknown is False
    assert usage.cache_read_tokens_unknown is True
    assert cache_stats_line(usage, usage.prompt_tokens) == "💾 Cache: unknown"


def test_f3_narrowness_a_present_payload_with_an_unknown_prompt_still_prints():
    """The r6 pins for an explicitly-unmeasured PROMPT must stay reachable."""
    usage = normalize_usage({
        "prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
        "prompt_tokens_unavailable": True, "unavailable": True,
    })
    assert usage.usage_unknown is False
    assert cache_stats_line(usage, usage.prompt_tokens) == "💾 Cache: unknown"


def test_f3_narrowness_measured_cache_with_an_unknown_prompt_is_still_unknown():
    """The refused percentage is the whole point of the unknown branch."""
    usage = normalize_usage({
        "prompt_tokens": None, "completion_tokens": 50,
        "prompt_tokens_unavailable": True,
        "prompt_tokens_details": {"cached_tokens": 80, "cache_creation_tokens": 0},
    })
    assert usage.cache_read_tokens == 80
    assert cache_stats_line(usage, usage.prompt_tokens) == "💾 Cache: unknown"


def test_f3_control_measured_paths_are_byte_identical():
    measured = normalize_usage({
        "prompt_tokens": 100, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 100},
    })
    assert cache_stats_line(measured, measured.prompt_tokens) == (
        "💾 Cache: 100/100 tokens (100% hit, 0 written)"
    )
    no_cache = normalize_usage({"prompt_tokens": 100, "completion_tokens": 50})
    assert cache_stats_line(no_cache, no_cache.prompt_tokens) is None


# --------------------------------------------------------------------------
# F12 — the renderer must be exercised on the row shape production produces
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["production", "get_turn"])
def test_f12_only_the_production_row_shape_reaches_the_cache_lookups(
    shape, tmp_path, monkeypatch
):
    """`_row_to_dict` renames cache_read/cache_write; production does not.

    The renderer reads `rec.get("cache_read", 0)`, so the renamed shape
    resolves every cache lookup to 0 and silently drops the `• Cached:` row.
    This test pins the divergence so a future assertion about cache or
    last-call lines cannot be written against a shape that never occurs.
    """
    import plugins.blackbox as bb
    import plugins.blackbox.store as store

    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "turns.db")
    call = _call(
        input_tokens=100, output_tokens=50, cache_read_tokens=900, total_tokens=1050
    )
    rec = bb._build_record(
        session_id="s", interrupted=False, model="claude-sonnet-4-5",
        platform="cli", provider="anthropic", user_message="", final_response="",
        turn_usage={**call, "calls": [call], "api_calls": 1},
        cfg={"store_text": False}, kwargs={},
    )
    store.insert_turn(rec)

    if shape == "get_turn":
        row = store.get_turn(rec.turn_id)
        assert "cache_read" not in row
        assert "• Cached:" not in _render(row)
    else:
        conn = sqlite3.connect(store._db_path())
        try:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (rec.turn_id,)
            ).fetchone())
        finally:
            conn.close()
        assert "cache_read" in row
        block = _render(row)
        assert "• Cached: 900/1k" in block
        assert "900 cache-read" in block
