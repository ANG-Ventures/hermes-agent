"""The loop-liveness watchdog must name the blocked site BEFORE it restarts.

Card t_71ea46ce: on 2026-09-24 the watchdog killed Apollo (exit 75,
reason=loop_liveness_watchdog) and the only attribution was an incidental
Discord heartbeat traceback. Now every missed probe logs
``PHASE=loop_liveness_blocked_site`` with the loop thread's innermost frame
and a full stack from ``sys._current_frames``; the hard exit comes after.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import patch

import pytest

from gateway.shutdown_watchdog import (
    _format_loop_thread_stack,
    start_loop_liveness_watchdog,
)


@pytest.fixture(autouse=True)
def _pin_host_as_unstarved():
    with (
        patch("gateway.shutdown_watchdog.os.getloadavg", return_value=(0.5, 0.5, 0.5)),
        patch("gateway.shutdown_watchdog.os.cpu_count", return_value=8),
    ):
        yield


def _walk_the_skills_tree_synchronously(release: threading.Event) -> None:
    # Stand-in for the 2026-09-24 rglob: a sync call that holds the loop.
    release.wait(timeout=5.0)


def test_watchdog_logs_blocked_site_and_stack_before_exit(caplog):
    loop = asyncio.new_event_loop()
    release = threading.Event()
    loop_ident: dict[str, int] = {}
    events: list[str] = []

    def run_loop():
        loop_ident["id"] = threading.get_ident()
        asyncio.set_event_loop(loop)
        loop.call_soon(_walk_the_skills_tree_synchronously, release)
        loop.run_forever()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    for _ in range(100):
        if "id" in loop_ident:
            break
        time.sleep(0.01)

    exited = threading.Event()

    def fake_exit(code):
        events.append(f"exit:{code}")
        exited.set()

    caplog.set_level(logging.ERROR, logger="gateway.shutdown_watchdog")
    orig_error = logging.getLogger("gateway.shutdown_watchdog").error

    def spy_error(msg, *a, **k):
        if "loop_liveness_blocked_site" in str(msg):
            events.append("site")
        return orig_error(msg, *a, **k)

    try:
        with (
            patch("gateway.shutdown_watchdog.logger.error", side_effect=spy_error),
            patch("gateway.shutdown_watchdog.faulthandler.dump_traceback"),
            patch("gateway.lifecycle_ledger.mark_exited", create=True),
            patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit),
        ):
            handle = start_loop_liveness_watchdog(
                loop,
                probe_interval=0.02,
                probe_timeout=0.05,
                max_strikes=2,
                loop_thread_id=loop_ident["id"],
            )
            assert handle is not None
            assert exited.wait(timeout=5.0), "watchdog never reached its exit"
            handle.stop()
            handle.join(timeout=2.0)
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2.0)
        loop.close()

    # attribution logged for each strike, and strictly before the exit
    assert events.index("site") < events.index("exit:75"), events
    assert events.count("site") >= 2
    text = caplog.text
    assert "PHASE=loop_liveness_blocked_site" in text
    assert "site=" in text and "_walk_the_skills_tree_synchronously" in text
    assert "loop thread stack:" in text
    assert "test_loop_liveness_blocked_site.py" in text


def test_format_loop_thread_stack_names_innermost_frame():
    ready = threading.Event()
    release = threading.Event()
    ident: dict[str, int] = {}

    def blocked_here():
        ident["id"] = threading.get_ident()
        ready.set()
        release.wait(timeout=5.0)

    t = threading.Thread(target=blocked_here, daemon=True)
    t.start()
    assert ready.wait(timeout=2.0)
    try:
        time.sleep(0.02)
        site, stack = _format_loop_thread_stack(ident["id"])
    finally:
        release.set()
        t.join(timeout=2.0)
    # innermost frame is inside threading.Event.wait; the caller is in the stack
    assert "threading.py" in site
    assert "blocked_here" in stack


def test_format_loop_thread_stack_never_raises_on_unknown_thread():
    assert _format_loop_thread_stack(None)[0] == "unknown"
    assert _format_loop_thread_stack(-12345)[0] == "unknown"
