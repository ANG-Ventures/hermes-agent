"""Subprocess entry for the S2 runner tests (``tests/cron/test_cron_script_runner.py``).

Runs ``cron.script_runner.main([])`` with test constants taken from the environment:

* ``RUNNER_HARNESS_CYCLE_S``     -> ``cron.lanes.RUNNER_CYCLE_S`` (ticker interval)
* ``RUNNER_HARNESS_WATCHDOG_S``  -> ``cron.lanes.RUNNER_TICK_WATCHDOG_S``
* ``RUNNER_HARNESS_GRACE_S``     -> ``cron.lanes.RUNNER_WATCHDOG_KILL_GRACE_S``
* ``RUNNER_HARNESS_BLOCK``       -> a path. While a file exists there, ``get_due_jobs`` (which runs
  AFTER ``.tick-script.lock`` is taken) blocks opening a FIFO nobody writes: a wedged tick that holds
  the script-lane lock (AC-4e, RC-3).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_block_stub(flag: Path) -> None:
    import cron.jobs as jobs
    import cron.scheduler as sched

    real = jobs.get_due_jobs
    fifo = flag.with_name(flag.name + ".fifo")

    def blocking_get_due_jobs(*args, **kwargs):
        if flag.exists():
            if not fifo.exists():
                os.mkfifo(fifo)
            (flag.parent / "blocked").write_text(str(os.getpid()), encoding="utf-8")
            with open(fifo, "rb"):  # blocks: no writer ever opens it
                pass
        return real(*args, **kwargs)

    jobs.get_due_jobs = blocking_get_due_jobs
    sched.get_due_jobs = blocking_get_due_jobs


def main() -> int:
    from cron import lanes

    for env, attr in (("RUNNER_HARNESS_CYCLE_S", "RUNNER_CYCLE_S"),
                      ("RUNNER_HARNESS_WATCHDOG_S", "RUNNER_TICK_WATCHDOG_S"),
                      ("RUNNER_HARNESS_GRACE_S", "RUNNER_WATCHDOG_KILL_GRACE_S")):
        if os.environ.get(env):
            setattr(lanes, attr, float(os.environ[env]))
    block = os.environ.get("RUNNER_HARNESS_BLOCK")
    if block:
        _install_block_stub(Path(block))
    from cron import script_runner

    return script_runner.main([])


if __name__ == "__main__":
    sys.exit(main())
