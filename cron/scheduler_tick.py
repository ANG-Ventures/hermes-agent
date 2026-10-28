"""In-process tick admission and dispatch; job execution stays in scheduler.py."""

import concurrent.futures
import contextlib
import time


def tick(verbose=True, adapters=None, loop=None, sync=True, *, can_dispatch=None, lane="all",
         report=None):
    """``lane`` (cron/lanes.py): ``"all"`` (default, both lanes), ``"agent"`` or ``"script"``.
    ``report``: optional callable that receives exactly one ``cron.lanes.TickOutcome`` per call
    that returns. The int return is unchanged."""
    from hermes_cli.backend_retirement import retirement
    from cron.lanes import LANES, TickOutcome, record_lock_wait

    if lane not in LANES:
        raise ValueError(f"unknown cron lane {lane!r}")

    def _report(outcome):
        if report is not None:
            with contextlib.suppress(Exception):
                report(outcome)

    started = time.monotonic()
    try:
        # Hold admission through the entire scan/advance/submit handoff. A predicate alone races
        # prepare after the check but before a due job enters the running-job ledger.
        with retirement.work() as admitted:
            if not admitted:
                _report(TickOutcome.REFUSED_RETIREMENT)
                return 0
            return _tick_admitted(
                verbose, adapters, loop, sync, can_dispatch=can_dispatch, lane=lane,
                report=_report)
    finally:
        record_lock_wait(f"tick:{lane}", time.monotonic() - started)


def _acquire_lane_locks(_sched, lane: str) -> dict:
    """Take the requested lane tick locks (non-blocking). Returns ``{lane: fd}`` for the locks
    acquired; a raise releases what was already taken. ``"all"`` takes ``.tick.lock`` first and
    gets nothing when it is busy: a ``.tick.lock`` holder (data-wipe quiesce, pre-S1 code) excludes
    the whole home, as before lanes. Only a busy ``.tick-script.lock`` narrows ``"all"`` to agent."""
    held: dict = {}
    try:
        for name in (("agent", "script") if lane == "all" else (lane,)):
            lock_dir, lock_file = _sched._get_lock_paths(name)
            _sched._ensure_cron_dir(lock_dir)
            lock_fd = _sched._acquire_tick_lock(lock_file)
            if lock_fd is not None:
                held[name] = lock_fd
            elif lane == "all" and name == "agent":
                break
    except BaseException:
        for lock_fd in held.values():
            _sched._release_tick_lock(lock_fd)
        raise
    return held


def _tick_admitted(
    verbose: bool = True, adapters=None, loop=None, sync: bool = True, *, can_dispatch=None,
    lane: str = "all", report=lambda _outcome: None):
    """Check and run all due jobs. File-locked so only one tick runs at a time (gateway ticker vs
    standalone daemon / manual tick). ``can_dispatch``: optional gate; false leaves due jobs for the
    next allowed tick. Returns the number of jobs executed (0 if another tick holds the lock)."""
    from cron import scheduler as _sched
    from cron.lanes import TickOutcome

    # Stale-code yield gate — BEFORE the lock race. A process whose checkout was updated under it
    # serves mixed sys.modules (jobs die on ImportErrors); if a fresher gateway holds the runtime
    # lock, ITS ticker dispatches. With no fresh holder (desktop-standalone) the tick proceeds.
    _skew = _sched._should_yield_tick_to_fresh_gateway()
    if _skew is not None:
        _sched._log_tick_yield_once(f"boot={_skew[0]} disk={_skew[1]}")
        raise _sched.CronTickYielded(_skew[0], _skew[1])

    # One lock per lane (.tick.lock / .tick-script.lock); "all" ticks the lanes it acquired.
    held = _acquire_lane_locks(_sched, lane)
    if not held:
        report(TickOutcome.LOCK_BUSY)
        return 0
    scan_lane = lane if lane != "all" or len(held) == 2 else next(iter(held))
    # Worktree GC and MCP orphan sweeps belong to the agent lane.
    agent_chores = scan_lane != "script"

    _dispatch_release = None
    try:
        # `hermes pause` ESTOP: skip dispatch, never touch in-flight runs; check_paused logs once.
        with contextlib.suppress(ImportError):
            from agent.estop import check_paused as _estop_check_paused
            if _estop_check_paused("cron", _sched.logger):
                report(TickOutcome.ESTOP)
                return 0

        if can_dispatch is not None and not can_dispatch():
            _sched.logger.debug("Cron dispatch paused while gateway drains existing work")
            report(TickOutcome.DISPATCH_PAUSED)
            return 0
        # A dying process must not scan for due jobs (fork #1307, t_1f4598ad). Anything it
        # dispatches is refused ("Skipped: cron scheduler is shutting down"), which records
        # last_status=error, advances next_run_at a whole period and consumes a restart_requeue
        # marker the next boot needed. Leave due jobs and markers for the next process's ticker.
        if _sched.is_shutting_down():
            _sched.logger.debug("Cron tick skipped: scheduler is shutting down")
            report(TickOutcome.SHUTTING_DOWN)
            return 0
        # Shared-checkout admission hold (gateway/checkout_admission.py): a gate exposing
        # ``admit()`` returns a release callable that must span the whole dispatch window, so a
        # hold engaged mid-tick still sees every job this tick registers (get_running_job_ids)
        # or refuses it.
        _dispatch_admit = getattr(can_dispatch, "admit", None)
        if callable(_dispatch_admit):
            _dispatch_release = _dispatch_admit()
            if _dispatch_release is None:
                _sched.logger.debug("Cron dispatch refused by shared-checkout admission hold")
                report(TickOutcome.ADMISSION_HOLD)
                return 0

        # Bot-chat drain and dead-owner reap were serialized by the one .tick.lock before lanes;
        # they run only on ticks that hold it (agent, all), never on the script lane.
        if "agent" in held:
            from cron.bot_chat_delivery import drain, drain_in_background
            if sync:
                drain()
            else:
                drain_in_background()
            _sched._maybe_reap_dead_owners()
        # Periodic worktree GC (6h, threaded) — the only sweep gateway-only boxes get.
        if agent_chores:
            try:
                _sched._maybe_run_worktree_maintenance()
            except Exception as _wt_exc:
                _sched.logger.debug("Worktree maintenance dispatch failed: %s", _wt_exc)

        due_jobs = _sched.get_due_jobs() if scan_lane == "all" else _sched.get_due_jobs(lane=scan_lane)
        report(TickOutcome.ADMITTED)
        # LOUD missed one-shot notices (fork #1087): a one-shot past its grace window is reported
        # to its delivery target instead of silently expiring.
        # Only the scanned lane's notices: a script tick must not send an agent job's notice.
        _sched._deliver_missed_oneshot_notices(adapters=adapters, loop=loop, lane=scan_lane)
        _sched._sweep_stale_inflight_for_tick(due_jobs)

        if not due_jobs:
            # Idle tick: skip config load + pool setup, but still reap crashed jobs' MCP orphans.
            if verbose:
                # Idle tick: skip config load + pool partitioning entirely (#33612 — the gateway ticker
                # calls tick(verbose=False) every 60s, so idle ticks previously fell through to
                # load_config()). Still run the post-tick MCP orphan sweep: main intentionally sweeps on
                # idle ticks so orphaned stdio children from crashed jobs are reaped even when nothing is
                # due.
                _sched.logger.info("%s - No jobs due", _sched._hermes_now().strftime('%H:%M:%S'))
            if agent_chores:
                _sched._sweep_mcp_orphans()
            return 0

        if verbose:
            _sched.logger.info("%s - %s job(s) due", _sched._hermes_now().strftime('%H:%M:%S'), len(due_jobs))

        # Advance next_run_at for recurring jobs FIRST, under the lock, before any execution
        # (at-most-once). Re-advancing running jobs keeps the grace window alive; mark_job_run
        # overwrites it on completion. Composes with the claim-time advance in claim_job_for_fire.
        _sched.advance_next_runs([job["id"] for job in due_jobs])

        _max_workers = _sched._resolve_max_parallel_workers()
        if verbose:
            _sched.logger.info(
                "Running %d job(s) in parallel (max_workers=%s)",
                len(due_jobs),
                _max_workers if _max_workers else "unbounded")

        def _process_job(job: dict) -> bool:
            return _sched._process_due_job(job, adapters, loop, verbose)

        # Persistent pool, non-blocking dispatch. Already-running jobs are skipped; mark_job_run
        # re-arms next_run_at on completion, so no catch-up queue is needed.
        _results: list = []
        _all_futures: list = []
        pool = _sched._get_parallel_pool(_max_workers)
        for job in due_jobs:
            fut = _sched._submit_with_guard(job, pool, _process_job)
            if fut is None:
                continue
            _all_futures.append(fut)
            if not sync:
                _results.append(True)  # optimistically counted

        if sync:
            for f in concurrent.futures.as_completed(_all_futures):
                try:
                    _results.append(f.result())
                except Exception as exc:
                    _sched.logger.error("Cron job future failed: %s", exc)
                    _results.append(False)
            if agent_chores:
                _sched._sweep_mcp_orphans()
            return sum(_results)

        if agent_chores:
            _sched._sweep_mcp_orphans_when_all_done(_all_futures)
        return sum(_results)
    finally:
        if _dispatch_release is not None:
            try:
                _dispatch_release()
            except Exception:
                _sched.logger.debug("cron dispatch admission release failed", exc_info=True)
        for lock_fd in held.values():
            _sched._release_tick_lock(lock_fd)
