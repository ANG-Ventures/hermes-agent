"""Subprocess entry for the S2 runner tests (``tests/cron/test_cron_script_runner.py``).

Runs ``cron.script_runner.main([])`` with test constants taken from the environment:

* ``RUNNER_HARNESS_CYCLE_S``     -> ``cron.lanes.RUNNER_CYCLE_S`` (ticker interval)
* ``RUNNER_HARNESS_WATCHDOG_S``  -> ``cron.lanes.RUNNER_TICK_WATCHDOG_S``
* ``RUNNER_HARNESS_GRACE_S``     -> ``cron.lanes.RUNNER_WATCHDOG_KILL_GRACE_S``
* ``RUNNER_HARNESS_BLOCK``       -> a path. While a file exists there, ``get_due_jobs`` (which runs
  AFTER ``.tick-script.lock`` is taken) blocks opening a FIFO nobody writes: a wedged tick that holds
  the script-lane lock (AC-4e, RC-3).
* ``RUNNER_HARNESS_STALE_S``     -> ``cron.lanes.RUNNER_PROGRESS_STALE_S`` (S3 scaled clock)
* ``RUNNER_HARNESS_REARM_S``     -> ``cron.lanes.RUNNER_REARM_AFTER_S`` (self re-arm, t_302ab1e8)
* ``RUNNER_HARNESS_LEDGER_HOLD`` -> a path. While a file exists there, a thread holds the
  ``cron.executions`` ledger lock (t_302ab1e8).
* ``RUNNER_HARNESS_FAULT``       -> ``emfile:<path>`` | ``retirement:<path>``. While a file exists at
  ``<path>``, every tick raises ``OSError(EMFILE)`` / is refused by retirement admission, and
  ``<path>.hit`` is touched (S3 AC-4b).
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


def _install_fault(spec: str) -> None:
    import errno

    import cron.scheduler as sched
    from hermes_cli.backend_retirement import retirement

    kind, _, path = spec.partition(":")
    flag = Path(path)
    hit = flag.with_name(flag.name + ".hit")

    def faulting() -> bool:
        if flag.exists():
            hit.touch()
            return True
        return False

    if kind == "emfile":
        real_tick = sched.tick

        def emfile_tick(*args, **kwargs):
            if faulting():
                raise OSError(errno.EMFILE, "Too many open files")
            return real_tick(*args, **kwargs)

        sched.tick = emfile_tick
    elif kind == "retirement":
        real_acquire = retirement.acquire
        retirement.acquire = lambda: False if faulting() else real_acquire()
    else:
        raise SystemExit(f"unknown RUNNER_HARNESS_FAULT {spec!r}")


def _install_ledger_hold(flag: Path) -> None:
    """While ``flag`` exists, a thread named ``ledger-holder`` sits inside
    ``cron.executions._transaction`` (it owns the ledger lock), so every ledger call of the tick
    waits on it: the t_302ab1e8 trip shape. Removing the flag releases it."""
    import threading
    import time

    from cron import executions

    def hold_lock_for_test():
        while True:
            if flag.exists():
                with executions._transaction():
                    (flag.parent / "holding").write_text(str(os.getpid()), encoding="utf-8")
                    while flag.exists():
                        time.sleep(0.05)
            time.sleep(0.05)

    threading.Thread(target=hold_lock_for_test, name="ledger-holder", daemon=True).start()


def main() -> int:
    from cron import lanes

    for env, attr in (("RUNNER_HARNESS_CYCLE_S", "RUNNER_CYCLE_S"),
                      ("RUNNER_HARNESS_WATCHDOG_S", "RUNNER_TICK_WATCHDOG_S"),
                      ("RUNNER_HARNESS_GRACE_S", "RUNNER_WATCHDOG_KILL_GRACE_S"),
                      ("RUNNER_HARNESS_REARM_S", "RUNNER_REARM_AFTER_S")):
        if os.environ.get(env):
            setattr(lanes, attr, float(os.environ[env]))
    if os.environ.get("RUNNER_HARNESS_STALE_S"):
        lanes.RUNNER_PROGRESS_STALE_S = float(os.environ["RUNNER_HARNESS_STALE_S"])
    if os.environ.get("RUNNER_HARNESS_FAULT"):
        _install_fault(os.environ["RUNNER_HARNESS_FAULT"])
    block = os.environ.get("RUNNER_HARNESS_BLOCK")
    if block:
        _install_block_stub(Path(block))
    hold = os.environ.get("RUNNER_HARNESS_LEDGER_HOLD")
    if hold:
        _install_ledger_hold(Path(hold))
    from cron import script_runner

    return script_runner.main([])


if __name__ == "__main__":
    sys.exit(main())
