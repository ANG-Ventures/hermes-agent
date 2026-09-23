"""CLASS SWEEP: every renderer of LAST-CALL data answers from one shared gate.

r6 round-4 finding 6. ``context_used`` and the ``last_cache_*`` split describe
the turn's FINAL call only (``agent/turn_finalizer.py`` reads
``context_compressor.last_prompt_tokens``). When that call returned no usage
payload the stored value is a placeholder, so dividing it by the window
fabricates a measurement.

This PR hardened ``card.py``'s Tokens-in / Tokens-out / Cached lines and
``last_turn.py``'s window line, and left ``card.py``'s ``• Context:`` line
ungated — so ONE record rendered through the two renderers said opposite things
about the same fact::

    • Tokens: unknown in + unknown out
    • Context: 0/200k 🟢 (0% of model max)     <-- fabricated measurement
    • Cached: unknown

The finding's own note was that no test discriminated it (applying the fix left
the suite at 248-passed-vs-248). These are the discriminating pins, and they are
written as a CROSS-RENDERER AGREEMENT rather than as a per-site string snapshot:
a future third renderer of last-call data that does not consult the shared gate
fails ``test_every_last_call_renderer_agrees_on_one_record`` without anyone
having to remember to extend a list.

The gate itself is ``agent.usage_pricing.last_call_prompt_unknown`` — one
callable, so the renderers cannot drift, including on the NULL-legacy-row
fallback that the two sites previously each implemented (or failed to).
"""

import sqlite3

import pytest

from agent.usage_pricing import last_call_prompt_unknown
from plugins.blackbox.card import _record_from_row, render_card
from plugins.blackbox.last_turn import render_last_turn_record
from plugins.blackbox.record import TurnRecord

_WINDOW = 200_000


def _row(**over):
    """A stored row for a 5-call turn. ``context_used`` is the FINAL call's count."""
    row = {
        "turn_id": "t1",
        "profile": "Apollo",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "platform": "telegram",
        "chat_id": "C1",
        "chat_name": "general",
        "api_calls": 5,
        "tools": [],
        "input_tokens": 4000,
        "output_tokens": 120,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "context_used": 0,
        "context_length": _WINDOW,
        "cost_usd": 1.0,
        "cost_status": "estimated",
        "ts_start": 1.0,
        "ts_end": 2.0,
        "latency_s": 1.0,
        "last_call_prompt_unknown": 1,
    }
    row.update(over)
    return row


def _both_renderings(row):
    """The same record through BOTH last-call renderers."""
    return {
        "card": render_card(_record_from_row(row), 1.0),
        "last_turn": "\n".join(render_last_turn_record(dict(row))),
    }


def _speaks_unknown_window(text):
    """Does this rendering describe the context window as unmeasured?"""
    for line in text.splitlines():
        if "ontext" not in line:
            continue
        if "unknown" in line:
            return True
        # A numeric occupancy for the window IS the fabricated measurement.
        if any(ch.isdigit() for ch in line.split(":", 1)[-1]):
            return False
    return None


# ---------------------------------------------------------------------------
# The discriminating pins.
# ---------------------------------------------------------------------------


def test_card_context_line_refuses_an_unmeasured_final_call():
    """The exact shipped defect: `• Context: 0/200k 🟢 (0% of model max)`."""
    text = render_card(_record_from_row(_row()), 1.0)

    assert "• Context: unknown/200k" in text
    assert "0/200k" not in text
    assert "0% of model max" not in text, (
        "an unmeasured final call must not be rendered as an empty, healthy window"
    )


def test_card_context_line_still_renders_a_measured_final_call():
    """MEASURED control — the gate must be narrow."""
    text = render_card(
        _record_from_row(_row(context_used=100_000, last_call_prompt_unknown=0)), 1.0
    )

    assert "• Context: 100k/200k" in text
    assert "50% of model max" in text
    assert "unknown" not in text.split("• Context:", 1)[1].splitlines()[0]


def test_every_last_call_renderer_agrees_on_one_record():
    """CLASS invariant: no two renderers of one record may disagree.

    This is the assertion that generalises past the two known sites. It is not a
    string snapshot of either renderer — it asks each rendering the same
    QUESTION ("did you describe the window as unmeasured?") and requires the
    answers to match the shared gate's verdict.
    """
    for unknown_flag in (1, 0):
        row = _row(
            last_call_prompt_unknown=unknown_flag,
            context_used=0 if unknown_flag else 100_000,
        )
        expected = last_call_prompt_unknown(row)
        assert expected is bool(unknown_flag)

        answers = {
            name: _speaks_unknown_window(text)
            for name, text in _both_renderings(row).items()
        }
        assert set(answers.values()) == {expected}, (
            f"renderers disagree for last_call_prompt_unknown={unknown_flag}: {answers}"
        )


def test_the_turn_level_flag_does_not_blank_a_measured_final_call():
    """The r6-finding-9 direction, held on the card too.

    The turn-level ``*_unknown`` flags are ABSORBING (`any()` across every call
    of the turn), so a 5-call turn whose call #2 returned no usage sets them even
    when the final call is fully measured. Gating the window on those would blank
    real provider numbers — which is why the shared gate reads the per-last-call
    column and only FALLS BACK to the turn flag.
    """
    row = _row(
        context_used=100_000,
        last_call_prompt_unknown=0,
        input_tokens_unknown=1,
        usage_unknown=0,
    )
    assert last_call_prompt_unknown(row) is False

    for name, text in _both_renderings(row).items():
        assert _speaks_unknown_window(text) is False, name
        assert "100k/200k" in text or "100,000" in text, name


def test_a_legacy_null_row_falls_back_to_the_turn_flag_in_both_renderers():
    """NULL is not False.

    ``store.py`` ALTERs this column in without a DEFAULT, so pre-column rows hold
    SQL NULL and ``TurnRecord.last_call_prompt_unknown`` — typed ``bool`` — carries
    that ``None`` straight through ``_record_from_row``. A bare truth test on
    ``None`` silently asserts "the final call was measured" about every historic
    row. The shared gate falls back to the turn-level flag instead, i.e. exactly
    the behaviour those rows had before the column existed.
    """
    legacy_unknown = _row(last_call_prompt_unknown=None, usage_unknown=1, context_used=0)
    assert _record_from_row(legacy_unknown).last_call_prompt_unknown is None
    assert last_call_prompt_unknown(legacy_unknown) is True
    for name, text in _both_renderings(legacy_unknown).items():
        assert _speaks_unknown_window(text) is True, name

    legacy_measured = _row(last_call_prompt_unknown=None, context_used=100_000)
    assert last_call_prompt_unknown(legacy_measured) is False
    for name, text in _both_renderings(legacy_measured).items():
        assert _speaks_unknown_window(text) is False, name


def test_the_gate_reads_both_record_shapes():
    """One callable over a dict ROW and a hydrated TurnRecord.

    ``last_turn.py`` renders a raw ``SELECT *`` dict; ``card.py`` renders a
    ``TurnRecord``. Duplicating the rule per shape is how the two drifted, so the
    gate must answer identically for both.
    """
    for flag, expected in ((1, True), (0, False), (None, True)):
        row = _row(last_call_prompt_unknown=flag, usage_unknown=1)
        assert last_call_prompt_unknown(row) is expected
        assert last_call_prompt_unknown(_record_from_row(row)) is expected


def test_the_flag_survives_a_real_store_round_trip(tmp_path, monkeypatch):
    """Executed against the real schema, not a hand-built dict.

    A display gate is only as good as the column feeding it, and this column is
    added by an ALTER on existing DBs.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib

    import hermes_constants

    importlib.reload(hermes_constants)
    import plugins.blackbox.store as store

    importlib.reload(store)

    rec = TurnRecord(
        turn_id="rt1",
        profile="Apollo",
        provider="anthropic",
        model="claude-opus-5",
        platform="telegram",
        chat_id="C1",
        chat_name="general",
        api_calls=5,
        tools=[],
        input_tokens=4000,
        output_tokens=120,
        context_used=0,
        context_length=_WINDOW,
        cost_usd=1.0,
        cost_status="estimated",
        ts_start=1.0,
        ts_end=2.0,
        last_call_prompt_unknown=True,
    )
    store.insert_turn(rec)

    stored = store.get_turn("rt1")
    assert stored is not None
    assert stored["last_call_prompt_unknown"] == 1
    assert last_call_prompt_unknown(stored) is True
    for name, text in _both_renderings(stored).items():
        assert _speaks_unknown_window(text) is True, name
