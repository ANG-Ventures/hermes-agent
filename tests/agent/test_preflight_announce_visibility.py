"""Only announce a compaction that actually compacts.

Ace reported a `📦 lcm maintenance compaction ... This may take a moment.` banner
with no `🗜️ Context compacted` stats message after it. The agent announced work,
went quiet, and reported nothing.

Measured specimen, session 20260807_183558_0c25fadb, 2026-08-08 02:26:38:

    02:26:38.442  compression started  messages=214  tokens=~164,784
    02:26:38.613  compression done     messages=214->214

214 -> 214 in 171ms. No `LCM compaction #` line at all. 4 of 21 compressions in
the log removed zero messages (19%).

CAUSE: the cleanup arm of should_compress_preflight returns True for sanitize-only
ingest-cleanup adoption -- deterministic, already durable, no summarizer call,
folds nothing. It must still RUN, but it is not a compaction and must not announce
itself as one. The stats renderer then has a zero delta and emits nothing, so the
user is left with a banner and silence.
"""

import inspect

import pytest


# ── engine side: the visibility flag ────────────────────────────────────────


class _Engine:
    """Carrier for the two engine methods under test."""

    def __init__(self):
        self._last_compression_status = "idle"
        self._last_compression_noop_reason = ""
        self._last_preflight_reason = ""


def _mark(engine, reason="", **kw):
    from plugins.context_engine.lcm.engine import LCMEngine

    return LCMEngine._mark_preflight_compression_requested(engine, reason, **kw)


def _visible(engine):
    from plugins.context_engine.lcm.engine import LCMEngine

    return LCMEngine.preflight_is_user_visible(engine)


def test_a_real_compaction_is_announced():
    e = _Engine()
    assert _mark(e, "compactable backlog outside the fresh tail") is True
    assert _visible(e) is True


def test_sanitize_only_cleanup_is_silent():
    """The specimen: cleanup adoption runs but must not announce."""
    e = _Engine()
    assert _mark(e, "an attachment was moved to external storage",
                 user_visible=False) is True
    assert _visible(e) is False


def test_the_pass_still_runs_when_silent():
    """Silencing the banner must not stop the work.

    An externalized payload should not linger in active context just because we
    declined to narrate the cleanup.
    """
    e = _Engine()
    assert _mark(e, "cleanup", user_visible=False) is True  # still True == still runs
    assert e._last_compression_status == "pending"


def test_overflow_recovery_through_the_cleanup_arm_stays_visible():
    """A forced-overflow pass is real work even when cleanup also fired."""
    e = _Engine()
    assert _mark(e, "context overflow recovery", user_visible=True) is True
    assert _visible(e) is True


def test_default_is_visible_for_back_compat():
    """Any caller that omits the kwarg keeps today's behavior."""
    e = _Engine()
    _mark(e, "some reason")
    assert _visible(e) is True


def test_engine_without_the_flag_reads_visible():
    """A never-marked engine must not be silently muted."""
    class _Bare:
        pass

    assert _visible(_Bare()) is True


# ── wiring: the flag must reach the real decision ───────────────────────────


def test_cleanup_arm_marks_itself_not_user_visible():
    """The cleanup branch must actually PASS user_visible.

    Without this the flag exists, the unit tests above all pass, and production
    announces exactly as before -- the inert-fix shape that shipped twice in this
    same subsystem (PR #506, then its config bridge).
    """
    from plugins.context_engine.lcm.compaction import CompactionMixin

    src = inspect.getsource(CompactionMixin.should_compress_preflight)
    assert "user_visible=" in src, (
        "the cleanup arm must mark its visibility or the hook is inert"
    )


def test_host_consults_the_visibility_hook_before_announcing():
    """turn_context must CALL the hook, not merely tolerate it existing."""
    from agent import turn_context

    src = inspect.getsource(turn_context)
    assert "preflight_is_user_visible" in src, (
        "the host must consult the engine before emitting compaction status"
    )
    # and the call must gate the emit, not sit after it
    emit_idx = src.index("_engine_preflight_status)")
    hook_idx = src.index("preflight_is_user_visible")
    assert hook_idx < emit_idx, "visibility must be resolved BEFORE the emit"


def test_a_broken_hook_fails_open_to_announcing():
    """A raising hook must never silence a genuine compaction."""
    from agent import turn_context

    src = inspect.getsource(turn_context)
    hook_idx = src.index("preflight_is_user_visible")
    window = src[hook_idx:hook_idx + 900]
    assert "except Exception" in window, "the hook call must be guarded"
    assert "_preflight_visible = True" in window, (
        "the guard must default to announcing, never to silence"
    )


@pytest.mark.parametrize("reason", [
    "compactable backlog outside the fresh tail",
    "ignored-message backlog outside the fresh tail",
    "context overflow recovery",
    "deferred maintenance backlog",
])
def test_non_cleanup_arms_stay_visible(reason):
    """Only the sanitize-only cleanup arm is silenced; everything else speaks."""
    e = _Engine()
    _mark(e, reason)
    assert _visible(e) is True
