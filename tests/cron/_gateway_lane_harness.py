"""Subprocess entry for the S3 two-process tests (``tests/cron/test_cron_lane_handoff.py``): a
gateway-role cron ticker, i.e. ``InProcessCronScheduler.start(delegate_script_lane=True)`` over the
process home, as ``gateway/run.py`` starts it, without the rest of the gateway.

* ``RUNNER_HARNESS_CYCLE_S``  -> the ticker interval
* ``RUNNER_HARNESS_STALE_S``  -> ``cron.lanes.RUNNER_PROGRESS_STALE_S`` (scaled clock)
* ``GATEWAY_HARNESS_LANES``   -> a jsonl file; every ``tick()`` call appends ``{"t", "lane"}``
  (``lane`` null = ``"all"``)
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    import cron.scheduler as sched
    from cron import lanes
    from cron.scheduler_provider import InProcessCronScheduler

    if os.environ.get("RUNNER_HARNESS_STALE_S"):
        lanes.RUNNER_PROGRESS_STALE_S = float(os.environ["RUNNER_HARNESS_STALE_S"])
    interval = float(os.environ.get("RUNNER_HARNESS_CYCLE_S") or lanes.RUNNER_CYCLE_S)
    log = os.environ.get("GATEWAY_HARNESS_LANES")
    real_tick = sched.tick

    def logged_tick(*args, **kwargs):
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "lane": kwargs.get("lane")}) + "\n")
        return real_tick(*args, **kwargs)

    sched.tick = logged_tick
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_a: stop.set())
    InProcessCronScheduler().start(stop, interval=interval, delegate_script_lane=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
