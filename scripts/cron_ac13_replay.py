#!/usr/bin/env python3
"""AC-13 lock-contention replay and the P0-5 healthy-tick measurement (cron script runner spec
v1.0 §8 AC-13, §7 Phase 0 P0-5, D15).

One run:
  * a scratch host root (``<out>/run-NN/home/.hermes``) with the knob on and a SANITIZED copy of
    a root ``jobs.json``: every enabled job, every target ``local``, every script replaced by a stub
    that sleeps the job's median duration (cap 30 s; 1 s with no history), monitor/env/workdir/
    interpreter/profile fields dropped;
  * the busiest minute of the store (croniter over the next 24 h, script lane) is replayed at
    ``t0``: each job's first fire inside ``[M, M + cycles + 1 min)`` is shifted to ``t0 + offset``;
    every other job is parked a day out;
  * a runner (``cron.script_runner.main``, lane ``script``) and a gateway-role ticker
    (``InProcessCronScheduler.start(delegate_script_lane=True)``, the ``gateway/run.py`` shape; it
    ticks ``agent`` while it delegates) with agent runs stubbed to 5 s, ``RUNNER_CYCLE_S`` real;
  * ``cycles`` runner ticks after ``t0``, then both process groups are killed.

Each child dumps its §5.2 ring (``cron.lanes.lock_waits()``) plus a timestamped tick log every
0.5 s. Thresholds are fixed here, before any run (``THRESHOLDS``): max ``_jobs_lock`` wait < 5 s in
every section, 0 ``CronLockDegraded``, 0 30-s lock timeouts, AC-3 duplicate query = 0.

``--control`` adds the negative control: a helper holds ``<cron>/.jobs.lock`` for 6 s at
``t0 + 20 s``; the probe must then report a wait >= 5 s (the threshold trips). Control runs are
never written to the P0-5 jsonl.

P0-5: ``--repeats N`` healthy runs; every ``("tick:script", duration_s)`` after ``t0`` goes to
``--p05-out`` (jsonl) and the summary prints p50 / p99 / T_max and
``RUNNER_TICK_WATCHDOG_S = max(30, ceil(4 x T_max))``. A repeat that fails a threshold measures
contention, not health: the measurement is then reported invalid.

Read-only against the source store: ``--jobs`` is copied, never opened for write. Never point
``--out`` at a live home.

  python scripts/cron_ac13_replay.py --jobs /path/to/root/jobs.json --out ~/.hermes/var/oneshot/ac13-<card> \
      --repeats 10 --control --p05-out ~/.hermes/var/oneshot/p0-5-<card>.jsonl
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve()

THRESHOLDS = {
    "max_section_wait_s": 5.0,       # strict: every section's max wait must be < this
    "cron_lock_degraded": 0,
    "lock_timeouts_30s": 0,
    "ac3_duplicates": 0,
}
CONTROL_HOLD_S = 6.0
CONTROL_AT_S = 20.0
STUB_CAP_S = 30.0
STUB_DEFAULT_S = 1.0
AGENT_STUB_S = 5.0
RUN_HARD_TIMEOUT_S = 600.0
_KEEP = ("id", "name", "prompt", "schedule", "schedule_display", "no_agent", "enabled", "state",
         "model", "provider", "skills", "skill", "repeat", "timeout_s", "created_at")


# --- store preparation ---------------------------------------------------------------------------

def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def median_durations(jobs: list, executions_db: Path | None) -> dict:
    """``{job_id: seconds}``: median over the last 50 completed rows when an executions.db is
    given, else the last run's dispatched_at -> last_run_at from the store record."""
    out: dict = {}
    if executions_db is not None:
        conn = sqlite3.connect(f"file:{executions_db}?mode=ro", uri=True, timeout=30)
        try:
            for job in jobs:
                rows = conn.execute(
                    "SELECT started_at, finished_at FROM executions WHERE job_id = ? AND "
                    "status = 'completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
                    "ORDER BY finished_at DESC LIMIT 50", (job["id"],)).fetchall()
                vals = [(_parse(b) - _parse(a)).total_seconds() for a, b in rows
                        if _parse(a) and _parse(b)]
                vals = [v for v in vals if v >= 0]
                if vals:
                    out[job["id"]] = statistics.median(vals)
        finally:
            conn.close()
        return out
    for job in jobs:
        a = _parse((job.get("last_dispatch") or {}).get("dispatched_at"))
        b = _parse(job.get("last_run_at"))
        if a and b and (b - a).total_seconds() >= 0:
            out[job["id"]] = (b - a).total_seconds()
    return out


def _fires(job: dict, start: datetime, end: datetime) -> list:
    from croniter import croniter

    sched = job.get("schedule") or {}
    kind = sched.get("kind")
    out = []
    if kind == "cron":
        it = croniter(sched["expr"], start - timedelta(seconds=1))
        while True:
            t = it.get_next(datetime)
            if t >= end:
                break
            out.append(t)
    elif kind == "once":
        t = _parse(sched.get("run_at") or job.get("next_run_at"))
        if t is not None:
            if t.tzinfo is None:
                t = t.replace(tzinfo=start.tzinfo)
            if start <= t < end:
                out.append(t)
    elif kind == "interval" and sched.get("minutes"):
        step = timedelta(minutes=float(sched["minutes"]))
        t = _parse(job.get("next_run_at")) or start
        if t.tzinfo is None:
            t = t.replace(tzinfo=start.tzinfo)
        if t < start:
            t += step * math.ceil((start - t) / step)
        while t < end:
            out.append(t)
            t += step
    return out


def busiest_minute(jobs: list, tz) -> tuple:
    """(minute start, job ids due in it) over the script lane, next 24 h from today 00:00."""
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    counts: dict = {}
    for job in jobs:
        if job.get("no_agent") is not True or not job.get("enabled", True):
            continue
        for t in _fires(job, start, end):
            key = t.replace(second=0, microsecond=0)
            counts.setdefault(key, []).append(job["id"])
    minute = max(counts, key=lambda k: (len(counts[k]), -k.timestamp()))
    return minute, counts[minute]


def build_store(src_jobs: list, durations: dict, home: Path, minute: datetime, t0: datetime,
                window_s: float) -> dict:
    """Write the sanitized store and stub scripts under *home*; return a manifest."""
    (home / "cron").mkdir(parents=True)
    scripts = home / "scripts"
    scripts.mkdir()
    out_jobs, due_at_t0, in_window = [], [], 0
    far = (t0 + timedelta(days=1)).isoformat()
    for job in src_jobs:
        if not job.get("enabled", True):
            continue
        clean = {k: job[k] for k in _KEEP if k in job}
        clean["deliver"] = "local"
        clean["state"] = "scheduled"
        if job.get("no_agent") is True:
            secs = min(STUB_CAP_S, durations.get(job["id"], STUB_DEFAULT_S))
            name = f"stub_{job['id']}.sh"
            (scripts / name).write_text(f"#!/bin/sh\nsleep {secs:.3f}\n", encoding="utf-8")
            (scripts / name).chmod(0o755)
            clean["script"] = name
        else:
            clean["script"] = None
        kind = (job.get("schedule") or {}).get("kind")
        nxt = far
        fires = _fires(job, minute, minute + timedelta(seconds=window_s))
        if fires:
            offset = (fires[0] - minute).total_seconds()
            at = t0 + timedelta(seconds=offset)
            nxt = at.isoformat()
            in_window += 1
            if offset < 60 and job.get("no_agent") is True:
                due_at_t0.append(job["id"])
            if kind == "cron":
                # The due scan re-anchors a next_run_at that is not on the expr's lattice
                # (expr_edit), so a shifted fire needs an expr whose lattice holds it: daily at
                # the shifted UTC minute (the scratch home runs in UTC).
                clean["schedule"] = {"kind": "cron", "expr": f"{at.minute} {at.hour} * * *",
                                     "display": f"{at.minute} {at.hour} * * *"}
        elif kind == "once":
            continue  # a one-shot outside the window would only raise overdue notices
        elif kind == "cron":
            parked = t0 + timedelta(days=1)
            clean["schedule"] = {"kind": "cron", "expr": f"{parked.minute} {parked.hour} * * *",
                                 "display": f"{parked.minute} {parked.hour} * * *"}
        if kind == "once":
            clean["schedule"] = dict(job["schedule"], run_at=nxt)
        clean["next_run_at"] = nxt
        out_jobs.append(clean)
    from cron import store_pair

    def _seed(jobs: list) -> bool:
        jobs[:] = out_jobs
        return True

    store_pair.publish_definitions(home, _seed)
    (home / "config.yaml").write_text(
        "timezone: UTC\ncron:\n  oneshot_catchup_s: 0\n  script_runner:\n    enabled: true\n", encoding="utf-8")
    # due_first_minute = script-lane jobs due in t0's minute: what the runner must dispatch.
    return {"jobs": len(out_jobs), "due_first_minute": len(due_at_t0), "in_window": in_window,
            "script_stubs": sum(1 for j in out_jobs if j.get("no_agent") is True)}


# --- child roles -----------------------------------------------------------------------------------

_PEAKS: dict = {}
_PEAKS_LOCK = threading.Lock()
_TICK_SAMPLE = threading.local()  # the tick:* sample the current thread's tick() recorded


def _dumper(path: Path, ticks: list, stop: threading.Event) -> None:
    from cron import lanes

    while True:
        with _PEAKS_LOCK:
            peaks = {k: list(v) for k, v in _PEAKS.items()}
        data = {"pid": os.getpid(), "lock_waits": lanes.lock_waits(), "peaks": peaks,
                "ticks": list(ticks)}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
        if stop.wait(0.5):
            return


def _instrument(dump: Path, role: str) -> None:
    """Wrap ``cron.scheduler.tick`` to log (wall start, lane, ring entry) and start the dumper."""
    import cron.scheduler as sched
    from cron import lanes

    ticks: list = []
    real_tick = sched.tick
    real_record = lanes.record_lock_wait

    def record_lock_wait(label, seconds):
        # Every caller imports record_lock_wait from cron.lanes at call time, so this sees every
        # sample; the ring is bounded (10,000) and a peak must not be evicted before the dump.
        with _PEAKS_LOCK:
            cur = _PEAKS.setdefault(str(label), [0, 0.0])
            cur[0] += 1
            cur[1] = max(cur[1], float(seconds))
        if str(label).startswith("tick:"):
            _TICK_SAMPLE.entry = (str(label), float(seconds))
        real_record(label, seconds)

    lanes.record_lock_wait = record_lock_wait

    def logged_tick(*args, **kwargs):
        started = time.time()
        outcome: list = []
        user_report = kwargs.get("report")

        def report(o):
            outcome.append(getattr(o, "value", str(o)))
            if user_report is not None:
                user_report(o)

        kwargs["report"] = report
        _TICK_SAMPLE.entry = None
        try:
            return real_tick(*args, **kwargs)
        finally:
            # tick() records its own ("tick:<lane>", duration) in its finally, on this thread.
            entry = getattr(_TICK_SAMPLE, "entry", None)
            _TICK_SAMPLE.entry = None
            ticks.append({"t": started, "lane": kwargs.get("lane", "all"), "role": role,
                          "ring": entry, "outcome": outcome[0] if outcome else None})

    sched.tick = logged_tick
    threading.Thread(target=_dumper, args=(dump, ticks, threading.Event()), daemon=True,
                     name="ac13-dump").start()


def child_runner(dump: Path) -> int:
    _instrument(dump, "runner")
    from cron import script_runner

    return script_runner.main([])


def child_gateway(dump: Path) -> int:
    _instrument(dump, "gateway")
    import cron.scheduler as sched
    from cron import lanes
    from cron.scheduler_provider import InProcessCronScheduler

    real_run_job = sched.run_job

    def stub_run_job(job, **kwargs):
        if job.get("no_agent"):
            return real_run_job(job, **kwargs)
        time.sleep(AGENT_STUB_S)
        return True, "# ac13 stub\n", "ac13 stub agent run", None

    sched.run_job = stub_run_job
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_a: stop.set())
    InProcessCronScheduler().start(stop, interval=lanes.RUNNER_CYCLE_S, delegate_script_lane=True)
    return 0


def child_control(lock_path: Path, hold_s: float, marker: Path) -> int:
    import fcntl

    with open(lock_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        marker.write_text(json.dumps({"held_at": time.time()}), encoding="utf-8")
        time.sleep(hold_s)
        fcntl.flock(fh, fcntl.LOCK_UN)
    data = json.loads(marker.read_text(encoding="utf-8-sig"))
    data["released_at"] = time.time()
    marker.write_text(json.dumps(data), encoding="utf-8")
    return 0


# --- orchestration ---------------------------------------------------------------------------------

def _env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not (k.endswith("_API_KEY") or k.startswith("PYTEST_") or k in (
               "HERMES_MACHINE_ID", "_HERMES_CRON_EXTERNAL_WORKER", "INVOCATION_ID"))}
    env.update(PYTHONPATH=str(REPO_ROOT), HERMES_HOME=str(home), HOME=str(home.parent),
               PYTHONUNBUFFERED="1", HERMES_TIMEZONE="UTC")
    return env


def _spawn(argv: list, home: Path, log: Path) -> subprocess.Popen:
    fh = open(log, "a", encoding="utf-8")  # noqa: SIM115
    try:
        return subprocess.Popen([sys.executable, str(HERE)] + argv, cwd=str(REPO_ROOT),
                                env=_env(home), stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                                start_new_session=True)
    finally:
        fh.close()


def _kill(procs: list) -> None:
    for p in procs:
        if p.poll() is not None:
            continue  # reaped: its pid (and pgid) may already belong to someone else
        with contextlib.suppress(Exception):
            # start_new_session made each child a group leader: kill the whole group on POSIX.
            os.killpg(os.getpgid(p.pid), signal.SIGKILL) if hasattr(os, "killpg") else p.kill()
    for p in procs:
        with contextlib.suppress(Exception):
            p.wait(timeout=10)


def _load_dump(path: Path) -> dict:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {"lock_waits": [], "ticks": []}


def _ac3(home: Path) -> list:
    db = home / "cron" / "executions.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        return [list(r) for r in conn.execute(
            "SELECT job_id, scheduled_instant, COUNT(*) c FROM executions WHERE "
            "scheduled_instant IS NOT NULL AND started_at IS NOT NULL GROUP BY 1,2 HAVING c > 1")]
    finally:
        conn.close()


def _rows(home: Path) -> dict:
    db = home / "cron" / "executions.db"
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        return {f"{pid}:{status}": c for pid, status, c in conn.execute(
            "SELECT pid, status, COUNT(*) FROM executions GROUP BY 1,2")}
    finally:
        conn.close()


def one_run(args, src_jobs, durations, tz, run_dir: Path, *, control: bool) -> dict:
    from cron import lanes

    cycle = float(lanes.RUNNER_CYCLE_S)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    home = run_dir / "home" / ".hermes"
    minute, _ids = busiest_minute(src_jobs, tz)
    warmup = 2 * cycle
    t0 = (datetime.now(timezone.utc) + timedelta(seconds=warmup + 60)).replace(
        second=0, microsecond=0)  # on a minute boundary: cron fires sit on the lattice
    manifest = build_store(src_jobs, durations, home, minute, t0, (args.cycles + 1) * 60.0)
    manifest.update(minute=minute.isoformat(), t0=t0.isoformat(), control=control,
                    cycles=args.cycles,
                    cycle_s=cycle, thresholds=THRESHOLDS)
    procs: list = []
    started = time.monotonic()
    try:
        runner = _spawn(["--child", "runner", "--dump", str(run_dir / "runner.json")], home,
                        run_dir / "runner.log")
        procs.append(runner)
        # Deployment order: the runner is up and progressing before the gateway boots. Started in
        # the same second, the gateway's first "all" tick and the runner's tick phase-lock: the
        # runner reads LOCK_BUSY every cycle, never records last_ok_at, and the gateway never
        # delegates (observed 2026-10-28, /tmp/t0d4-ctl; reported on the card).
        deadline = time.monotonic() + 3 * cycle
        while not (lanes.read_runner_heartbeat(home) or {}).get("last_ok_at"):
            if time.monotonic() > deadline or runner.poll() is not None:
                manifest["aborted"] = "runner never progressed before the gateway boot"
                break
            time.sleep(0.2)
        gateway = _spawn(["--child", "gateway", "--dump", str(run_dir / "gateway.json")], home,
                         run_dir / "gateway.log")
        procs.append(gateway)
        manifest.update(runner_pid=runner.pid, gateway_pid=gateway.pid)
        control_proc = None
        control_at = None
        t0_epoch = t0.timestamp()
        while not manifest.get("aborted"):
            if time.monotonic() - started > RUN_HARD_TIMEOUT_S:
                manifest["aborted"] = "hard timeout"
                break
            if control and control_at is None:
                first = [t for t in _load_dump(run_dir / "runner.json")["ticks"]
                         if t["t"] >= t0_epoch - 1.0 and t["lane"] == "script"]
                if first:  # t0 + 20 s, t0 = the tick that dispatches the busiest minute
                    control_at = first[0]["t"] + CONTROL_AT_S
                    manifest["control_at"] = control_at
            if control and control_proc is None and control_at and time.time() >= control_at:
                control_proc = _spawn(["--child", "control", "--lock",
                                       str(home / "cron" / ".jobs.lock"),
                                       "--marker", str(run_dir / "control.json")],
                                      home, run_dir / "control.log")
                procs.append(control_proc)
            post = [t for t in _load_dump(run_dir / "runner.json")["ticks"]
                    if t["t"] >= t0_epoch - 1.0 and t["lane"] == "script"]
            control_done = (not control) or (control_proc is not None
                                             and control_proc.poll() is not None)
            if len(post) >= args.cycles and control_done:
                time.sleep(2.0)
                break
            for p in (runner, gateway):
                if p.poll() is not None:
                    manifest["aborted"] = f"pid {p.pid} exited {p.returncode}"
            if manifest.get("aborted"):
                break
            time.sleep(0.5)
    finally:
        _kill(procs)
    return evaluate(run_dir, home, manifest)


def evaluate(run_dir: Path, home: Path, manifest: dict) -> dict:
    t0_epoch = datetime.fromisoformat(manifest["t0"]).timestamp()
    sections: dict = {}
    tick_rows = []
    for role in ("runner", "gateway"):
        dump = _load_dump(run_dir / f"{role}.json")
        for label, (n, peak) in (dump.get("peaks") or {}).items():
            if label.startswith("tick:"):
                continue
            sections[f"{role}:{label}"] = {"n": n, "max_s": float(peak)}
        for t in dump["ticks"]:
            if t.get("ring"):
                tick_rows.append({"role": role, "t": t["t"], "after_t0": t["t"] >= t0_epoch - 1.0,
                                  "label": t["ring"][0], "duration_s": t["ring"][1],
                                  "outcome": t.get("outcome"), "lane": t.get("lane")})
    logs = "".join((run_dir / f"{r}.log").read_text(encoding="utf-8-sig", errors="replace")
                   for r in ("runner", "gateway") if (run_dir / f"{r}.log").exists())
    measured = {
        "max_section_wait_s": max((s["max_s"] for s in sections.values()), default=0.0),
        "cron_lock_degraded": logs.count("CronLockDegraded"),
        "lock_timeouts_30s": logs.count("waiting for the cron jobs lock"),
        "ac3_duplicates": len(_ac3(home)),
    }
    script_after = [r for r in tick_rows if r["role"] == "runner" and r["label"] == "tick:script"
                    and r["after_t0"]]
    executions = _rows(home)
    # Rows the runner dispatched (claimed, running or finished): a claimed row is queued behind
    # the worker pool when the run ends, which is the tick's work done, not missing workload.
    runner_started = sum(c for k, c in executions.items()
                         if k.split(":")[0] == str(manifest.get("runner_pid")))
    # Exercised, not vacuous: every post-t0 runner tick ADMITTED and the runner started at least
    # the first minute's due jobs.
    exercised = (len(script_after) >= int(manifest.get("cycles") or 0)
                 and all(r["outcome"] == "ADMITTED" for r in script_after)
                 and runner_started >= int(manifest.get("due_first_minute") or 0) > 0)
    passed = (measured["max_section_wait_s"] < THRESHOLDS["max_section_wait_s"]
              and measured["cron_lock_degraded"] == 0 and measured["lock_timeouts_30s"] == 0
              and measured["ac3_duplicates"] == 0 and not manifest.get("aborted") and exercised)
    result = dict(manifest, measured=measured, thresholds_pass=passed, sections=sections,
                  ticks=tick_rows, executions=executions, runner_started=runner_started,
                  exercised=exercised, script_ticks_after_t0=len(script_after),
                  admitted_after_t0=sum(1 for r in script_after if r["outcome"] == "ADMITTED"))
    if manifest.get("control"):
        # A probe wait cannot exceed the hold (the helper takes the lock first, so every waiter
        # arrives after it), so the spec's literal "max >= 6 s" for a 6 s hold is unreachable;
        # the control proves the probe trips the < 5 s threshold, and that the section that
        # tripped it overlapped the hold (reported, spec ambiguity flagged on the card).
        with contextlib.suppress(OSError, ValueError):
            result["control_hold"] = json.loads((run_dir / "control.json").read_text(encoding="utf-8-sig"))
        result["control_pass"] = (
            "released_at" in (result.get("control_hold") or {})
            and measured["max_section_wait_s"] >= THRESHOLDS["max_section_wait_s"])
    (run_dir / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def _pct(vals: list, q: float) -> float:
    vals = sorted(vals)
    if not vals:
        return float("nan")
    k = max(0, min(len(vals) - 1, math.ceil(q * len(vals)) - 1))
    return vals[k]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--child", choices=("runner", "gateway", "control"))
    ap.add_argument("--dump")
    ap.add_argument("--lock")
    ap.add_argument("--marker")
    ap.add_argument("--jobs", help="source root jobs.json (read only)")
    ap.add_argument("--executions-db", help="source executions.db for median durations (read only)")
    ap.add_argument("--out", help="artifacts dir")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=1,
                    help="healthy repeats run side by side (separate scratch homes)")
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--control", action="store_true", help="also run the negative control")
    ap.add_argument("--control-only", action="store_true")
    ap.add_argument("--p05-out", help="P0-5 jsonl output")
    ap.add_argument("--tz", default="America/Los_Angeles")
    args = ap.parse_args(argv)
    if args.child == "runner":
        return child_runner(Path(args.dump))
    if args.child == "gateway":
        return child_gateway(Path(args.dump))
    if args.child == "control":
        return child_control(Path(args.lock), CONTROL_HOLD_S, Path(args.marker))
    if not (args.jobs and args.out):
        ap.error("--jobs and --out are required")
    sys.path.insert(0, str(REPO_ROOT))
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(args.tz)
    data = json.loads(Path(args.jobs).read_text(encoding="utf-8-sig"))
    src_jobs = data["jobs"] if isinstance(data, dict) else data
    durations = median_durations(src_jobs, Path(args.executions_db) if args.executions_db else None)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    minute, ids = busiest_minute(src_jobs, tz)
    summary = {"source": str(args.jobs), "duration_source": args.executions_db or "jobs.json last run",
               "busiest_minute": minute.isoformat(), "busiest_count": len(ids),
               "thresholds": THRESHOLDS, "runs": []}
    healthy = []
    n_runs = 0 if args.control_only else args.repeats
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = [pool.submit(one_run, args, src_jobs, durations, tz, out / f"run-{i + 1:02d}",
                            control=False) for i in range(n_runs)]
        results = [f.result() for f in futs]
    for i, res in enumerate(results):
        summary["runs"].append({k: res.get(k) for k in (
            "measured", "thresholds_pass", "script_ticks_after_t0", "admitted_after_t0",
            "runner_pid", "aborted", "executions", "runner_started", "exercised")})
        print(f"run {i + 1}: pass={res['thresholds_pass']} {res['measured']} "
              f"script_ticks={res['script_ticks_after_t0']} admitted={res['admitted_after_t0']}",
              flush=True)
        healthy.append(res)
    if args.control or args.control_only:
        res = one_run(args, src_jobs, durations, tz, out / "control", control=True)
        summary["control"] = {k: res.get(k) for k in (
            "measured", "control_pass", "control_hold", "aborted")}
        print(f"control: control_pass={res['control_pass']} {res['measured']}", flush=True)
    if healthy:
        samples = []
        for i, res in enumerate(healthy, 1):
            for r in res["ticks"]:
                if r["role"] == "runner" and r["label"] == "tick:script" and r["after_t0"]:
                    samples.append({"repeat": i, "pid": res["runner_pid"], "t": r["t"],
                                    "label": r["label"], "duration_s": r["duration_s"],
                                    "outcome": r["outcome"]})
        durs = [s["duration_s"] for s in samples]
        t_max = max(durs) if durs else float("nan")
        valid = all(r["thresholds_pass"] for r in healthy)
        summary["p0_5"] = {"n": len(durs), "p50_s": _pct(durs, 0.5), "p99_s": _pct(durs, 0.99),
                           "t_max_s": t_max, "valid": valid,
                           "watchdog_s": max(30, math.ceil(4 * t_max)) if durs else None}
        if args.p05_out:
            p = Path(args.p05_out).expanduser()
            if not valid:  # contended data never lands at the path a watchdog is derived from
                p = p.with_name(p.stem + ".INVALID" + p.suffix)
            summary["p0_5"]["jsonl"] = str(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                for s in samples:
                    fh.write(json.dumps(s) + "\n")
        print("P0-5:", json.dumps(summary["p0_5"]), flush=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    ok = all(r["thresholds_pass"] for r in healthy) and summary.get("control", {}).get(
        "control_pass", True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
