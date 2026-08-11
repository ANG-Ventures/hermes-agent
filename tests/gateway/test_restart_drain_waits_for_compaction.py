"""A restart must not amputate an in-flight context compaction.

Measured incident (2026-08-10): a manual /compress on a 792-message session
started 21:35:18. A gateway restart armed by an UNRELATED session delivered
SIGTERM at 21:36:31 — 73 seconds in. The session append could not complete and
the user was told session storage failed and to check for a full disk, on a box
with 6.1 TiB free.

Root cause: ``/compress`` builds a throwaway ``AIAgent`` and runs it on an
executor, so it never appears in ``_running_agents``. The gateway's restart
drain (``_active_work_count``) therefore counted ZERO active work and entered
stop() immediately, despite ``restart_after_turn_timeout`` being 21600s and
willing to wait. Identical bug shape to in-flight cron work (#60432).
"""

from __future__ import annotations

import threading

import pytest

import agent.conversation_compression as cc


@pytest.fixture(autouse=True)
def _reset_counter():
    cc._COMPACTIONS_IN_FLIGHT = 0
    yield
    cc._COMPACTIONS_IN_FLIGHT = 0


class _Beat(cc._CompressionActivityHeartbeat):
    """Heartbeat with the thread + agent side effects stubbed out."""

    def __init__(self):
        self._agent = None
        self._commit_fence = None
        self._counted = False
        self._suppressed = False
        self._interval_seconds = 60.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: None, daemon=True)

    def _touch(self, *a, **kw):
        pass

    def _should_suppress(self):
        return False


def test_counter_is_zero_when_nothing_is_compacting():
    assert cc.compactions_in_flight() == 0


def test_start_makes_the_compaction_visible():
    b = _Beat()
    b.start()
    assert cc.compactions_in_flight() == 1


def test_stop_clears_it():
    b = _Beat()
    b.start()
    b.stop()
    assert cc.compactions_in_flight() == 0


def test_double_stop_does_not_underflow():
    """stop() is called twice on some timeout paths."""
    b = _Beat()
    b.start()
    b.stop()
    b.stop()
    assert cc.compactions_in_flight() == 0


def test_a_suppressed_stop_still_releases_the_count():
    """A detached/suppressed episode must not pin the drain for the full cap."""
    b = _Beat()
    b.start()
    b._suppressed = True
    b._should_suppress = lambda: True
    b.stop()
    assert cc.compactions_in_flight() == 0, (
        "a suppressed stop leaked the count — a restart would wait out the "
        "entire after-turn cap on a compaction that already finished"
    )


def test_concurrent_compactions_are_counted_independently():
    beats = [_Beat() for _ in range(3)]
    for b in beats:
        b.start()
    assert cc.compactions_in_flight() == 3
    beats[0].stop()
    assert cc.compactions_in_flight() == 2
    beats[1].stop()
    beats[2].stop()
    assert cc.compactions_in_flight() == 0


def test_counter_is_threadsafe_under_parallel_start_stop():
    def worker():
        for _ in range(50):
            b = _Beat()
            b.start()
            b.stop()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cc.compactions_in_flight() == 0


# --- the gateway side ------------------------------------------------------


def test_active_work_count_includes_compactions():
    """The drain must SEE it — the whole point of the fix."""
    from gateway.run import GatewayRunner

    class _G:
        _running_agent_count = lambda self: 0
        _active_cron_job_count = lambda self: 0
        _active_api_run_count = lambda self: 0
        _active_compaction_count = GatewayRunner._active_compaction_count
        _active_work_count = GatewayRunner._active_work_count

    g = _G()
    assert g._active_work_count() == 0

    b = _Beat()
    b.start()
    try:
        assert g._active_compaction_count() == 1
        assert g._active_work_count() == 1, (
            "the restart drain is blind to an in-flight compaction — a "
            "sibling session's restart will amputate it"
        )
    finally:
        b.stop()

    assert g._active_work_count() == 0


def test_active_work_count_source_contract():
    """Source contract: the term must stay in the sum.

    An arithmetic test passes trivially when nothing is compacting, so it
    cannot catch someone dropping the term.
    """
    import inspect
    from gateway.run import GatewayRunner

    src = inspect.getsource(GatewayRunner._active_work_count)
    assert "_active_compaction_count()" in src, (
        "compaction work was dropped from the restart drain's total"
    )
