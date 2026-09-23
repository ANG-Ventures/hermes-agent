"""The loop-liveness CRITICAL must name host LOAD, not just the missed probes.

2026-09-20 incident: a kanban worker's runaway busy-loops drove the host to load
average 538 on 32 cores. The resident gateway's loop-liveness watchdog fired
twice with "Gateway event loop missed 3 consecutive liveness probes; exiting
with code 75" — a line that is true and useless. It describes the SYMPTOM
(the loop did not answer) and omits the one number that names the CAUSE, so the
first 55 minutes of the incident were spent looking for a deadlock in the
gateway rather than at ``uptime``.

Attaching ``load1=`` and ``ncpu=`` to that same line costs one ``os.getloadavg``
call on a path that is already hard-exiting, and makes the next occurrence
self-diagnosing from the log alone.
"""

from __future__ import annotations

import asyncio
import re
import threading
from unittest.mock import MagicMock, patch

from gateway.shutdown_watchdog import start_loop_liveness_watchdog


def test_missed_probe_critical_carries_load1_and_ncpu():
    """The CRITICAL emitted before hard-exit names host load and cpu count.

    The exit path is only reached for a WEDGED classification, so the host load
    sample is pinned (``_sample_host_load``) rather than read from the real
    machine. That pin is not cosmetic: on a busy CI runner the real load can
    exceed ``max(2 * ncpu, 8)``, at which point ``evaluate_liveness_miss``
    correctly classifies the miss as STARVED and *holds* instead of exiting —
    so the watchdog never reaches ``os._exit`` and the old 10s deadline on
    ``exited.wait`` expired. Measured 2026-09-22: sample ``(12.0, 4)`` ->
    never exits; ``(0.5, 4)`` -> exits in 0.06s. That was a load-dependent
    PRODUCTION BRANCH, not scheduling jitter, and the starved branch has its
    own coverage in tests/gateway/test_liveness_starvation_hold.py.

    The assertion is the ``exited`` witness plus the message content — never
    elapsed time. The wait bound below is a deadlock backstop only.
    """
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    # Never schedules the probe callback -> the probe always times out.
    loop.call_soon_threadsafe.side_effect = lambda callback: None
    exited = threading.Event()
    messages: list[str] = []

    def _record(msg, *args, **_kwargs):
        try:
            messages.append(msg % args if args else str(msg))
        except Exception:
            messages.append(str(msg))

    with (
        patch("gateway.shutdown_watchdog.logger.critical", side_effect=_record),
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback"),
        # Unloaded host -> WEDGED -> the exit path under test. Pinned for
        # both the classifier (_sample_host_load) and the message formatter
        # (_host_load_suffix reads os.getloadavg / os.cpu_count directly).
        patch("gateway.shutdown_watchdog.os.getloadavg", return_value=(0.50, 0.40, 0.30)),
        patch("gateway.shutdown_watchdog.os.cpu_count", return_value=4),
        patch(
            "gateway.shutdown_watchdog.os._exit",
            side_effect=lambda _code: exited.set(),
        ),
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        assert handle is not None
        # Deadlock backstop, NOT an assertion about speed: the witness is
        # `exited`, and the classification above makes reaching it
        # load-independent.
        assert exited.wait(timeout=60.0), "watchdog never reached the exit path"
        handle.stop()

    joined = "\n".join(messages)
    assert "missed" in joined and "liveness probes" in joined
    assert re.search(r"load1=[0-9]+\.[0-9]+", joined), (
        "loop-liveness CRITICAL does not report the host 1-min load average; "
        f"got: {joined!r}"
    )
    assert re.search(r"ncpu=[0-9]+", joined), (
        f"loop-liveness CRITICAL does not report ncpu; got: {joined!r}"
    )


def test_exit_path_is_reachable_regardless_of_real_host_load():
    """Regression lock for the CI flake: the test above must not depend on the
    load of the machine it runs on.

    The watchdog samples the REAL host load to classify wedged-vs-starved. With
    that sample unpinned, the exit path is reachable on an idle box and
    unreachable on a busy one — which is exactly how this file red-lined
    unrelated PRs (ANG-Ventures/hermes-agent#811, 2026-09-22). Drive both
    classifications explicitly and assert the ORDERING fact (did the exit path
    run at all), never the time taken.
    """
    for load1, ncpu, should_exit in ((0.5, 4, True), (12.0, 4, False)):
        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        loop.call_soon_threadsafe.side_effect = lambda callback: None
        exited = threading.Event()
        held = threading.Event()

        def _maybe_held(msg, *args, **_kwargs):
            if "HOLDING" in str(msg):
                held.set()

        with (
            patch(
                "gateway.shutdown_watchdog.logger.critical", side_effect=_maybe_held
            ),
            patch("gateway.shutdown_watchdog.faulthandler.dump_traceback"),
            patch(
                "gateway.shutdown_watchdog._sample_host_load",
                return_value=(load1, ncpu),
            ),
            patch(
                "gateway.shutdown_watchdog.os._exit",
                side_effect=lambda _code: exited.set(),
            ),
        ):
            handle = start_loop_liveness_watchdog(
                loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
            )
            assert handle is not None
            try:
                # Wait on whichever witness is expected; both bounds are
                # deadlock backstops, not timing assertions.
                if should_exit:
                    assert exited.wait(timeout=60.0), (
                        f"load1={load1} ncpu={ncpu} is WEDGED by "
                        "evaluate_liveness_miss, so the exit path must run"
                    )
                else:
                    assert held.wait(timeout=60.0), (
                        f"load1={load1} ncpu={ncpu} is STARVED, so the "
                        "watchdog must emit the HOLDING critical"
                    )
                    assert not exited.is_set(), (
                        "a starved host must HOLD, not exit 75"
                    )
            finally:
                handle.stop()


def test_load_snapshot_degrades_gracefully_without_getloadavg():
    """A platform without ``os.getloadavg`` must not break the exit path."""
    from gateway import shutdown_watchdog as sw

    with patch.object(sw.os, "getloadavg", side_effect=OSError("unsupported")):
        text = sw._host_load_suffix()
    assert isinstance(text, str)
    assert "load1=" in text  # still emitted, as 'unknown'
