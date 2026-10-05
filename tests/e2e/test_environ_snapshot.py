"""``environ_snapshot`` survives env mutation on another thread.

e2e job 111551327218 (test_gateway_turn_liveness): a pool thread built a child
env with ``os.environ.items()`` while the main thread's hermetic fixture
set/deleted ``HERMES_HONCHO_HOST`` and died with ``KeyError``.
"""

from __future__ import annotations

import os
import threading

from tests.e2e.environ_snapshot import environ_snapshot

_VAR = "HERMES_E2E_ENVIRON_SNAPSHOT_RACE"


def test_snapshot_is_consistent_while_another_thread_sets_and_deletes():
    stop = threading.Event()

    def toggle():
        while not stop.is_set():
            os.environ[_VAR] = "1"
            os.environ.pop(_VAR, None)

    os.environ["HERMES_E2E_ENVIRON_SNAPSHOT_STABLE"] = "kept"
    toggler = threading.Thread(target=toggle, daemon=True)
    toggler.start()
    try:
        for _ in range(20_000):
            snap = environ_snapshot()
            assert snap["HERMES_E2E_ENVIRON_SNAPSHOT_STABLE"] == "kept"
            assert snap.get(_VAR, "1") == "1"
    finally:
        stop.set()
        toggler.join()
        os.environ.pop(_VAR, None)
        os.environ.pop("HERMES_E2E_ENVIRON_SNAPSHOT_STABLE", None)
