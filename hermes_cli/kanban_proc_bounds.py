"""Process-count bounds for dispatcher-spawned kanban workers (t_368e9873).

Two incidents, one gap: nothing bounded how many processes a worker's tree
could hold while the worker was alive.

* 2026-10-01 22:49 -> 02:10 PT: a scratch bench (``data/micro.py``, card
  t_164177ba) matched its own child argv and re-spawned itself 18,818 times
  (exec-flight-recorder). The uid's process table climbed 1,451 -> 10,340 of
  ``kern.maxprocperuid`` 10,666; every fork on the host then failed EAGAIN and
  the default gateway's watchdog exited it (63 cron results lost).
* 2026-09-29: a worker's pytest leaked 153 headless Chromes (load 250).

The existing reapers (exit path, crash path, reclaim/archive/timeout, terminal
workspace sweep) only act once a worker is GONE. This module adds the two
bounds that act while it is alive:

1. :func:`worker_nproc_preexec_limit` - ``setrlimit(RLIMIT_NPROC)`` in the
   spawn preexec. On Darwin and Linux RLIMIT_NPROC is checked against the
   whole UID's process count, not the caller's tree (measured on the Studio:
   soft limit = uid count + 40 allowed 47 forks, then EAGAIN). So the limit is
   a uid-wide CEILING for worker trees that keeps a reserve of slots the
   resident gateways and launchd jobs (which keep the full limit) can still
   fork into. Default ``auto`` = 75% of the inherited soft limit (8,000 of
   10,666 on the Studio). Healthy uid totals, 521 load-attribution samples
   2026-09-30..10-02 outside the storm: p50 1,420, p99 1,815, max 5,516.
2. :func:`enforce_worker_process_cap` - a dispatcher-tick census of each
   running worker's tree (its session members plus every process carrying its
   ``HERMES_KANBAN_TASK``+``HERMES_KANBAN_RUN_ID``). A run over
   ``kanban.worker_max_procs_per_run`` (default 256) is terminated, its whole
   tree reaped, and the card blocked ``capability``. Healthy per-run tree
   sizes, 207 ten-second samples over 42 min of 83 live runs on 2026-10-02:
   p50 7, p90 16, p99 74, max 74; 3 x p99 = 222, rounded up to 256. The storm
   grew about 120 procs/min, so a 60 s tick catches it within about 2 ticks of
   crossing the cap, thousands of slots before the uid limit.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import Counter
from typing import Any, Callable, Optional

import psutil

_log = logging.getLogger(__name__)

#: Fraction of the inherited RLIMIT_NPROC soft limit a worker tree may use.
WORKER_NPROC_AUTO_FRACTION = 0.75
#: Default per-run process cap (3 x measured p99 of 74, rounded up).
DEFAULT_WORKER_MAX_PROCS_PER_RUN = 256
#: Floor for an explicit per-run cap: below this a normal worker (agent,
#: execute_code kernel, LSP, a short pytest -n) would be killed.
MIN_WORKER_MAX_PROCS_PER_RUN = 32


def _kanban_cfg(kanban_cfg: Optional[dict]) -> dict:
    if kanban_cfg is not None:
        return kanban_cfg or {}
    try:
        from hermes_cli.config import load_config

        return load_config().get("kanban") or {}
    except Exception:
        return {}


def resolve_worker_nproc_limit(
    kanban_cfg: Optional[dict] = None,
    *,
    current: Optional[tuple[int, int]] = None,
) -> Optional[int]:
    """The RLIMIT_NPROC soft limit for a worker tree, or None (no change).

    ``kanban.worker_nproc_limit``: ``auto`` (default) = 75% of the inherited
    soft limit; a positive int = that value; ``0``/``off``/``false`` = off.
    Never RAISES the inherited limit, and an unlimited inherited soft limit
    with ``auto`` leaves it alone (there is no ceiling to take 75% of).
    """
    try:
        import resource
    except ImportError:  # Windows
        return None
    if not hasattr(resource, "RLIMIT_NPROC"):
        return None
    raw = _kanban_cfg(kanban_cfg).get("worker_nproc_limit", "auto")
    if raw is False or str(raw).strip().lower() in ("0", "off", "false", "none", "no"):
        return None
    try:
        soft, hard = current if current is not None else resource.getrlimit(resource.RLIMIT_NPROC)
    except (OSError, ValueError):
        return None
    inf = resource.RLIM_INFINITY
    if str(raw).strip().lower() in ("auto", ""):
        if soft == inf or soft <= 0:
            return None
        want = int(soft * WORKER_NPROC_AUTO_FRACTION)
    else:
        try:
            want = int(raw)
        except (TypeError, ValueError):
            # A typo must fail SAFE: keep the guard on with the auto value.
            if soft == inf or soft <= 0:
                return None
            want = int(soft * WORKER_NPROC_AUTO_FRACTION)
        if want <= 0:
            return None
    if soft != inf and want >= soft:
        return None
    return want


def worker_nproc_preexec_limit(limit: Optional[int]) -> Optional[Callable[[], None]]:
    """Return a child-side callable that lowers RLIMIT_NPROC to ``limit``.

    Lowers soft AND hard so a runaway descendant cannot raise it back.
    Best-effort: a denied setrlimit must never abort the spawn.
    """
    if limit is None or limit <= 0:
        return None

    def _apply() -> None:  # pragma: no cover - runs in the forked child
        try:
            import resource

            _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
            new_hard = limit if (hard == resource.RLIM_INFINITY or hard > limit) else hard
            resource.setrlimit(resource.RLIMIT_NPROC, (min(limit, new_hard), new_hard))
        except Exception:
            pass

    return _apply


def chain_preexec(*fns: Optional[Callable[[], None]]) -> Optional[Callable[[], None]]:
    """Combine preexec callables (None entries dropped), or None if empty."""
    live = [f for f in fns if f is not None]
    if not live:
        return None
    if len(live) == 1:
        return live[0]

    def _all() -> None:  # pragma: no cover - runs in the forked child
        for f in live:
            f()

    return _all


def worker_max_procs_per_run(kanban_cfg: Optional[dict] = None) -> Optional[int]:
    """Per-run process cap from ``kanban.worker_max_procs_per_run``.

    Default 256. ``0``/``off`` disables. Values below 32 are raised to 32.
    An unparseable value keeps the default (fail safe: guard stays on).
    """
    raw = _kanban_cfg(kanban_cfg).get(
        "worker_max_procs_per_run", DEFAULT_WORKER_MAX_PROCS_PER_RUN)
    if raw is False or str(raw).strip().lower() in ("0", "off", "false", "none", "no"):
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WORKER_MAX_PROCS_PER_RUN
    if val <= 0:
        return None
    return max(val, MIN_WORKER_MAX_PROCS_PER_RUN)


def census_worker_trees(
    runs: dict[tuple[str, str], int],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Count each run's processes in ONE pass over the uid's process table.

    ``runs`` maps ``(task_id, str(run_id))`` -> worker pid (= session id).
    A process belongs to a run when its session id is the worker's pid, or
    its environment carries the run's task+run id (children that setsid out
    of the session keep the env), or it has no run identity in its
    environment and its ``--user-data-dir`` names the card (Linux Chrome).
    Session members that are, or descend from, a process older than the
    worker are a previous holder of a recycled sid and are not counted.
    Same uid only; self excluded.
    Returns ``{key: {"procs": n, "top": [(name, n), ...]}}``.
    """
    if not runs or not hasattr(os, "getuid"):
        return {}
    from hermes_cli import kanban_db as kb

    by_sid = {int(pid): key for key, pid in runs.items() if pid}
    by_card = {key[0]: key for key in runs}
    # Generation check: a session id outlives its leader, so a recycled
    # worker pid could inherit an older session's members, and those keep
    # forking. A genuine member is born after the worker (its leader) and so
    # is every same-session ancestor; 1 s slack for clock granularity. An
    # unreadable worker birth trusts the sid match.
    leader_birth = {sid: kb._member_birth(sid) for sid in by_sid}
    uid = os.getuid()  # windows-footgun: ok (hasattr-gated above)
    me = os.getpid()
    members: dict[tuple[str, str], Counter] = {k: Counter() for k in runs}
    # pid -> (sid, ppid, birth) for every same-uid process, for the walk.
    table: dict[int, tuple[Optional[int], int, Optional[float]]] = {}
    sid_hits: list[tuple[int, int, str]] = []
    for proc in psutil.process_iter(
            ["pid", "ppid", "uids", "name", "cmdline", "create_time"]):
        info = proc.info
        pid = info.get("pid") or 0
        uids = info.get("uids")
        if pid <= 1 or pid == me or uids is None or uids.real != uid:
            continue
        try:
            sid: Optional[int] = os.getsid(pid)
        except OSError:
            sid = None
        table[pid] = (sid, info.get("ppid") or 0, info.get("create_time"))
        name = info.get("name") or "?"
        if sid in by_sid:
            sid_hits.append((pid, sid, name))
            continue
        try:
            env = proc.environ()
        except (psutil.Error, OSError):
            continue
        if env.get("HERMES_KANBAN_TASK") is None:
            # Linux Chrome erases its environ window; its profile path still
            # names the card (kb._cmdline_profile_cards).
            cards = kb._cmdline_profile_cards(info.get("cmdline"))
            key = next((by_card[c] for c in cards if c in by_card), None)
            # The card names no run: a browser older than this run's worker
            # belongs to an earlier run of the card, not this one.
            lead = leader_birth.get(runs[key]) if key is not None else None
            born = info.get("create_time")
            if lead is not None and born is not None and born < lead - 1.0:
                key = None
        else:
            key = (env.get("HERMES_KANBAN_TASK"), env.get("HERMES_KANBAN_RUN_ID"))
        if key in members:
            members[key][name] += 1

    def _stale(pid: int, sid: int) -> bool:
        lead = leader_birth.get(sid)
        if lead is None:
            return False
        seen: set[int] = set()
        while pid in table and pid not in seen and pid != sid:
            seen.add(pid)
            p_sid, ppid, born = table[pid]
            if p_sid != sid:
                return False
            if born is not None and born < lead - 1.0:
                return True
            pid = ppid
        return False

    for pid, sid, name in sid_hits:
        if not _stale(pid, sid):
            members[by_sid[sid]][name] += 1
    return {
        k: {"procs": sum(c.values()), "top": c.most_common(3)}
        for k, c in members.items()
    }


def enforce_worker_process_cap(
    conn,
    *,
    cap: Optional[int] = None,
    census: Callable = census_worker_trees,
    notify: bool = True,
) -> list[str]:
    """Kill + block every host-local running worker whose tree exceeds ``cap``.

    Termination goes through ``_terminate_reclaimed_worker`` (owner-identity
    check, SIGTERM -> SIGKILL, then the session reap and the run-env escapee
    reap), so a recycled pid is never signalled and the whole tree is
    reaped. Afterwards the card is blocked ``capability`` fenced on the run,
    with a ``process_cap_exceeded`` event naming the count and top commands.
    Returns the task ids capped this tick.
    """
    from hermes_cli import kanban_db as kb

    if cap is None:
        cap = worker_max_procs_per_run()
    if cap is None:
        return []
    host_prefix = f"{kb._claimer_id().split(':', 1)[0]}:"
    rows = conn.execute(
        "SELECT id, worker_pid, claim_lock, current_run_id FROM tasks "
        "WHERE status = 'running' AND worker_pid IS NOT NULL "
        "AND current_run_id IS NOT NULL"
    ).fetchall()
    runs: dict[tuple[str, str], int] = {}
    meta: dict[tuple[str, str], Any] = {}
    for row in rows:
        if not (row["claim_lock"] or "").startswith(host_prefix):
            continue
        key = (row["id"], str(row["current_run_id"]))
        runs[key] = int(row["worker_pid"])
        meta[key] = row
    if not runs:
        return []
    counts = census(runs)
    capped: list[str] = []
    for key, c in counts.items():
        if c["procs"] <= cap:
            continue
        row = meta[key]
        tid, pid, run_id = row["id"], int(row["worker_pid"]), int(row["current_run_id"])
        top = ", ".join(f"{name} x{n}" for name, n in c["top"])
        _log.warning(
            "kanban: worker tree of %s run %s holds %d processes > cap %d (%s); terminating",
            tid, run_id, c["procs"], cap, top,
        )
        termination = kb._terminate_reclaimed_worker(
            pid, row["claim_lock"],
            owner_window=kb._worker_owner_window(conn, tid, pid, run_id),
            conn=conn, task_id=tid, run_id=run_id,
        )
        # The worker can be gone while its tree is not (it was the session
        # leader, its descendants are not); _terminate_reclaimed_worker only
        # reaps after proving the worker dead, so a refused kill leaves the
        # tree for the next tick and the card untouched.
        payload = {
            "pid": pid, "procs": c["procs"], "cap": cap, "top": c["top"],
            "terminated": bool(termination.get("terminated")),
            "sigkill": bool(termination.get("sigkill")),
            "session_groups_reaped": termination.get("session_groups_reaped", 0),
            "env_escapees_reaped": termination.get("env_escapees_reaped", 0),
        }
        try:
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "process_cap_exceeded", payload, run_id=run_id)
        except Exception as exc:
            _log.debug("process_cap_exceeded event failed: %s", exc)
        if not termination.get("terminated") or termination.get("identity_mismatch"):
            # Refused kill: the tree stays for the next tick. Recycled pid:
            # the recorded worker is gone and the crash path owns the card.
            continue
        reason = (
            f"process cap: worker tree held {c['procs']} processes > "
            f"kanban.worker_max_procs_per_run={cap} ({top}); the run was "
            "terminated and its tree reaped. Fix the runaway (a self-respawning "
            "script, a leaked browser/pytest pool) or raise the cap, then unblock."
        )
        try:
            blocked = kb.block_task(
                conn, tid, reason=reason, kind="capability", expected_run_id=run_id)
        except Exception as exc:
            blocked = False
            _log.warning("kanban: process-cap block of %s failed: %s", tid, exc)
        if not blocked:
            # The tree is already reaped; the card was not blocked (DB locked,
            # run moved on). Do not page a block that did not happen: the
            # crash path sees the dead worker next tick, and a respawned
            # runaway trips the cap again.
            _log.warning("kanban: process-cap kill of %s run %s not followed by a block", tid, run_id)
            continue
        capped.append(tid)
        if notify:
            _notify_cap(tid, run_id, c["procs"], cap, top)
    return capped


def _notify_cap(tid: str, run_id: int, procs: int, cap: int, top: str) -> None:
    """One #alerts line per capped run. Best-effort."""
    try:
        from hermes_cli import kanban_budget as _kbudget

        script = _kbudget._notify_script_path()
        if script is None:
            return
        body = (
            f"🧨 Kanban worker {tid} run {run_id}: process tree held {procs} "
            f"processes > cap {cap} ({top}). Terminated, tree reaped, card blocked "
            "capability (kanban.worker_max_procs_per_run)."
        )
        _kbudget._run_notify([sys.executable, script, "--sev", "warn", "--send", body[:1900]])
    except Exception as exc:  # pragma: no cover - paging must never break a tick
        _log.debug("process cap notify failed: %s", exc)
