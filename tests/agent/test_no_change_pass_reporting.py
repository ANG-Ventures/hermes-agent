"""A pass that compacted nothing must not claim it compacted something.

Ace: a `📦 lcm maintenance compaction … This may take a moment.` banner posted and
no stats message followed.

MEASURED (not assumed) — the renderer was asked what it produces for the real
specimen, session 20260807_183558_0c25fadb, 214 messages, 164,784 -> 164,377
tokens:

    🗜️ Context compacted: 214→214 messages · ~164K→~164K tokens · engine: lcm

So it does not go silent — it emits a FALSE CLAIM. Nothing was folded, no summary
was generated, 171ms elapsed, and the headline says "Context compacted". The
repeat passes are then swallowed by the announce dedupe key (same boundary, same
key), which is why the chat shows a "this may take a moment" banner and then
nothing.

Two defects, one cause: a zero-delta pass is rendered as a compaction.
"""

import inspect

import pytest

from agent.fork_ext.compaction_ext import (
    _format_compaction_announce,
    _format_no_change_close,
    _is_no_change_pass,
)


def _announce(**kw):
    """The measured specimen, overridable per test."""
    base = dict(
        engine_name="lcm",
        status="sanitized",
        old_session_id="s1",
        new_session_id="s1",
        old_messages=214,
        new_messages=214,
        pre_tokens=164_784,
        post_tokens=164_377,
        model="claude-opus-5",
        provider="claude-apr",
        in_place=True,
    )
    base.update(kw)
    return _format_compaction_announce(**base)


# ── the false claim ─────────────────────────────────────────────────────────


def test_the_specimen_no_longer_claims_a_compaction():
    """THE BUG: 214 -> 214 rendered as 'Context compacted'."""
    line = _announce()
    assert line is not None
    assert "Context compacted" not in line, (
        "a pass that folded nothing must not use the compaction headline"
    )
    assert "nothing to compact" in line


def test_the_close_is_still_informative():
    """Replacing a false claim with silence would be no better."""
    line = _announce()
    assert "214" in line
    assert "unchanged" in line


def test_sanitize_case_explains_the_externalized_payload():
    line = _announce(status="sanitized")
    assert "external storage" in line


def test_a_real_compaction_is_untouched():
    """The normal path must keep its full headline."""
    line = _announce(new_messages=101, post_tokens=61_267)
    assert line is not None
    assert "nothing to compact" not in line
    assert "Context compacted" in line


# ── the predicate: both axes must be flat ───────────────────────────────────


def test_message_drop_counts_as_real_work():
    assert _is_no_change_pass(214, 101, 164_784, 164_377) is False


def test_token_drop_alone_counts_as_real_work():
    """Tool-result pruning holds the message count but genuinely shrinks tokens."""
    assert _is_no_change_pass(214, 214, 164_784, 100_000) is False


def test_the_measured_specimen_is_a_no_change_pass():
    """407 of 164,784 tokens (0.2%) is placeholder drift, not compaction."""
    assert _is_no_change_pass(214, 214, 164_784, 164_377) is True


def test_message_growth_is_not_a_no_op():
    """Growth is the summary-row signature (N folded into 1 appends a row).

    Any change in the message count means the transcript was restructured, so
    only an identical count can be a no-op.
    """
    assert _is_no_change_pass(214, 216, 164_784, 165_000) is False


@pytest.mark.parametrize("pre_t,post_t,expected", [
    (100_000, 100_000, True),    # identical
    (100_000, 99_900, True),     # 0.1% — drift
    (100_000, 99_000, True),     # 1.0% — at the floor, still drift
    (100_000, 98_000, False),    # 2.0% — real
])
def test_the_one_percent_floor(pre_t, post_t, expected):
    assert _is_no_change_pass(214, 214, pre_t, post_t) is expected


def test_missing_counts_do_not_trigger_the_close():
    """Unknown counts must fall through to the normal renderer, not guess."""
    assert _is_no_change_pass(None, None, None, None) is False
    assert _is_no_change_pass(0, 0, 0, 0) is False


def test_unknown_tokens_never_read_as_no_change():
    """CI regression: 1 -> 2 messages with tokens absent is NOT a no-op.

    Caught by tests/run_agent/test_413_compression.py. Folding messages into a
    summary row can GROW the message count while shrinking content, so message
    growth alone proves nothing. When the token axis is unknown the honest
    answer is "I cannot tell" — fall through rather than claim nothing happened.
    """
    assert _is_no_change_pass(1, 2, None, None) is False
    assert _is_no_change_pass(1, 2, 1_234, 0) is False
    assert _is_no_change_pass(1, 2, 0, 509) is False


def test_message_growth_with_a_token_drop_is_real_work():
    """The summary-row signature: +1 message, big token reduction."""
    assert _is_no_change_pass(1, 2, 1_234, 509) is False


def test_manual_compress_is_out_of_scope():
    """Manual /compress keeps its own feedback contract.

    apps/desktop/e2e/session-compression-and-queue-stop.spec.ts asserts
    /Compressed|No changes from compression/ on the manual path, and
    manual_compression_feedback.py is a deliberate carve-out from the silence
    rules. The reported bug was AUTOMATIC maintenance passes narrating
    themselves dishonestly; widening past that bends an unrelated contract.
    Caught by the Desktop E2E, not by unit tests.
    """
    line = _announce(trigger_reason="manual")
    assert line is not None
    assert "nothing to compact" not in line


def test_automatic_passes_are_still_gated():
    """The scope narrowing must not disable the fix for its actual target."""
    for reason in ("engine_preflight_maintenance", "threshold", "idle_resume", None):
        line = _announce(trigger_reason=reason)
        assert "nothing to compact" in line, f"{reason} must still be gated"


def test_garbage_counts_do_not_raise():
    assert _is_no_change_pass("x", None, [], {}) is False


# ── wiring ──────────────────────────────────────────────────────────────────


def test_the_predicate_gates_the_real_renderer():
    """The helper must be CALLED by the announce path, not merely exist.

    This subsystem has shipped three inert fixes whose unit tests all passed,
    each because the assertions stopped short of the real call path.
    """
    src = inspect.getsource(_format_compaction_announce)
    assert "_is_no_change_pass(" in src


def test_the_gate_runs_after_the_lcm_skip_checks():
    """Ordering: existing skip gating must still win, so we do not resurrect
    an announce that was deliberately suppressed."""
    src = inspect.getsource(_format_compaction_announce)
    assert src.index("_ANNOUNCE_STATUS_CONDITIONAL") < src.index("_is_no_change_pass(")


def test_close_survives_garbage_counts():
    line = _format_no_change_close(
        engine_name="lcm", pre_messages="x", post_messages=None,
        pre_tokens=None, post_tokens=None,
    )
    assert isinstance(line, str) and line
