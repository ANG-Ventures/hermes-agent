#!/usr/bin/env python3
"""Re-run the failed jobs of a CI run, and fall back to a full re-run when
GitHub refuses to start the selective attempt.

Why this exists (docs/ci/rerun-failed.md has the measurements):

``gh run rerun <id> --failed`` on this repo's ``ci.yaml`` sometimes creates an
attempt that concludes ``startup_failure`` with ZERO jobs, and the run page
shows GitHub's own "An unexpected error has occurred" annotation. No job
starts, so no step in the workflow can detect it or print an ``::error`` —
the only place to handle it is the caller of the re-run. A full
``gh run rerun <id>`` on the same run then starts normally.

``merge_group`` runs always get a full re-run: a selective re-run copies the
first attempt's ``detect`` and ``generate`` outputs instead of recomputing
them, so the retry would test the old lane set and slice plan.

Usage::

    python3 scripts/ci/rerun_failed.py --repo OWNER/REPO RUN_ID

Exit 0 when an attempt started (selective or fallback full), 1 when both
failed to start, 2 on bad input or a run that is not completed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], str]


def _gh(args: Sequence[str]) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def _api(run: Runner, path: str) -> dict:
    return json.loads(run(["api", path]))


def wait_for_start(
    run: Runner,
    repo: str,
    run_id: int,
    attempt: int,
    *,
    timeout_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> str:
    """Return ``started``, ``startup_failure`` or ``unknown`` for ``attempt``.

    ``started`` = the attempt has at least one job. ``startup_failure`` = it
    concluded that way with no jobs. ``unknown`` = neither within the timeout.
    """
    deadline = clock() + timeout_s
    base = f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"
    while True:
        try:
            info = _api(run, base)
        except subprocess.CalledProcessError:
            info = {}  # attempt row not created yet
        if info:
            if info.get("conclusion") == "startup_failure":
                return "startup_failure"
            jobs = _api(run, f"{base}/jobs?per_page=1")
            if jobs.get("total_count", 0) > 0:
                return "started"
        if clock() >= deadline:
            return "unknown"
        sleep(poll_s)


def rerun_failed(
    repo: str,
    run_id: int,
    *,
    run: Runner = _gh,
    timeout_s: float = 180,
    poll_s: float = 10,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out=sys.stdout,
) -> int:
    info = _api(run, f"repos/{repo}/actions/runs/{run_id}")
    if info.get("status") != "completed":
        print(f"::error::run {run_id} is {info.get('status')}; only a completed run can be re-run", file=out)
        return 2
    prev = int(info["run_attempt"])
    waits = dict(timeout_s=timeout_s, poll_s=poll_s, sleep=sleep, clock=clock)

    if info.get("event") == "merge_group":
        print(f"run {run_id} is merge_group: full re-run (a selective re-run reuses stale detect/generate outputs)", file=out)
        run(["run", "rerun", str(run_id), "--repo", repo])
        state = wait_for_start(run, repo, run_id, prev + 1, **waits)
        print(f"attempt {prev + 1}: {state}", file=out)
        return 1 if state == "startup_failure" else 0

    run(["run", "rerun", str(run_id), "--repo", repo, "--failed"])
    state = wait_for_start(run, repo, run_id, prev + 1, **waits)
    print(f"attempt {prev + 1} (failed jobs only): {state}", file=out)
    if state != "startup_failure":
        return 0

    print(
        f"::warning::attempt {prev + 1} of run {run_id} is startup_failure with 0 jobs "
        "(GitHub-side error on a failed-jobs re-run); falling back to a full re-run",
        file=out,
    )
    run(["run", "rerun", str(run_id), "--repo", repo])
    state = wait_for_start(run, repo, run_id, prev + 2, **waits)
    print(f"attempt {prev + 2} (full): {state}", file=out)
    if state == "startup_failure":
        print(f"::error::run {run_id}: full re-run also failed to start", file=out)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", required=True)
    p.add_argument("run_id", type=int)
    p.add_argument("--timeout", type=float, default=180)
    a = p.parse_args(argv)
    return rerun_failed(a.repo, a.run_id, timeout_s=a.timeout)


if __name__ == "__main__":
    sys.exit(main())
