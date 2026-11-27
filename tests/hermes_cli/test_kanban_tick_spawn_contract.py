"""Dispatcher tick round 2 (t_97a78e19): step attribution, maintenance off the tick, spawn-count contract.

Measured 2026-11-10 on the Studio (default gateway, runtime d1323fd5, load1 p50 124): tick wall p50 151 s, and
the gate-ticks record explained ~35 s of it. py-spy (40 in-tick samples): orphan sweep 7, PR reads 7, identity
probe 5, live-root walks 3; leaves: waiting on a child 12, realpath 7, fork/exec 6.

1. every board tick reports where its wall went (``steps_ms``) and the gateway line carries it;
2. the orphan sweep runs on a background thread at its cadence, never inline in a gateway tick;
3. after-merge PR reads are one ``gh`` per repo, not one per PR, with the same release verdicts;
4. worktree routing reads a repo's refs once per board tick, not two ``rev-parse`` per card;
5. the contract: a tick over a fixture board with N ready cards spawns a number of child processes that does not
   grow with N, and every call site that spawns from the tick is on :data:`ALLOWED_SPAWN_SITES`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

import pytest

from hermes_cli import kanban_after_merge as kam
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_gate as prg
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _make, _plan, _tick, kanban_home  # noqa: F401

_REAL_POPEN = subprocess.Popen
REPO = "ANG-Ventures/hermes-home"
# The gateway tick's flags. Read off the module so the contract test runs unchanged against a tree that predates
# them (the merge base, where it is shown red).
GATEWAY = {"defer_maintenance": True} if hasattr(kbd, "_orphan_sweep_off_tick") else {}

# Every call site allowed to start a child process from inside a dispatcher tick, by ``file:function``, with its
# cadence. A NEW synchronous spawn on the tick path fails test 5 until it is listed here with its reason. Keep the
# list short: the measured tick spent ~45 % of its wall forking or waiting on children (t_97a78e19).
ALLOWED_SPAWN_SITES = {
    # once per repo per tick: blocked-card PR gates, batched (t_45a4bee2)
    "kanban_pr_gate.py:_query_pr_chunk": "per repo per tick (PR-gate prefetch + after-merge reads)",
    # once per tick: the orphan sweep's cwd probe, only on the non-gateway path (gateway: background thread)
    "hermes_proc_cwd.py:_run_batch": "orphan sweep cadence (300 s), one child for all pids",
    # repo-context probes are cached per checkout signature (t_45a4bee2)
    "kanban_pr_gate.py:_git": "per checkout, cached by .git signature",
    # worktree routing: once per repo per board tick (t_97a78e19)
    "kanban_db.py:_git_out": "worktree toplevel/git-dir, memoized per board tick per path",
    "kanban_worktree_port.py:_read": "for-each-ref, once per repo per board tick",
    "kanban_worktree_port.py:origin_slug": "remote get-url, cached per repo (TTL)",
    "kanban_worktree_port.py:_clean_on_branch": "own .worktrees/<id> tree only",
    "kanban_worktree_port.py:_on_branch": "own .worktrees/<id> tree only",
    "kanban_worktree_port.py:_git": "ahead/merge-base, and the live tip re-read (Prism ce53b51a0bc2), for a card "
                                     "branch that exists locally: per routed card that already has one (a rerun), never a fresh card",
    # pool-host clone: background on a gateway tick (t_915b8d2d); the synchronous path is the one-shot CLI
    "kanban_worktree_port.py:clone_on_host": "per placed worktree card (background in the gateway)",
    # the spawn itself and its placement prep: per spawned worker, bounded by the spawn allowance, not by N
    "kanban_worker_hosts.py:_run_ident_probe": "per placed spawn, bounded by the allowance (not memoized: a "
                                               "per-card includeIf gitdir: can change the answer)",
}


def _site() -> str:
    for fr in reversed(traceback.extract_stack()[:-2]):
        f = fr.filename.replace("\\", "/")
        if "/tests/" in f or "/site-packages/" in f or "/lib/python" in f:
            continue
        if any(seg in f for seg in ("/hermes_cli/", "/gateway/", "/hermes_")):
            return f"{f.rsplit('/', 1)[-1]}:{fr.name}"
    return "?"


# Threads the tick hands work to and never waits on: their children are off the tick path (counted apart).
_OFF_TICK_THREADS = ("kanban-orphan-sweep", "kanban-clone")


class SpawnCounter:
    """Counts every child process started (``subprocess.Popen``) by its hermes call site; ``gh`` answers
    "cannot tell" without touching the network. ``sleep`` makes every spawn cost that many seconds. A spawn on a
    background pool the tick does not wait on lands in ``off_tick``, not ``sites``."""

    def __init__(self, sleep: float = 0.0):
        self.sites: Counter = Counter()
        self.off_tick: Counter = Counter()
        self.sleep = sleep
        self._lock = threading.Lock()

    def install(self, monkeypatch):
        counter = self

        class Counting(_REAL_POPEN):
            def __init__(self, args, *a, **k):
                off = threading.current_thread().name.startswith(_OFF_TICK_THREADS)
                with counter._lock:
                    (counter.off_tick if off else counter.sites)[_site()] += 1
                if counter.sleep:
                    time.sleep(counter.sleep)
                if isinstance(args, (list, tuple)) and args and str(args[0]) in ("gh", "ssh"):
                    args = [sys.executable, "-c", "import sys; sys.exit(1)"]
                super().__init__(args, *a, **k)

        monkeypatch.setattr(subprocess, "Popen", Counting)
        return self

    @property
    def total(self) -> int:
        return sum(self.sites.values())


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def anchor(tmp_path):
    repo = tmp_path / "anchor"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", f"https://github.com/{REPO}.git", cwd=repo)
    kwt._origin_cache.clear()
    return repo


@pytest.fixture
def board(kanban_home, monkeypatch, anchor):
    """A board with every measured spawn source per card: a ready scratch card, a ready worktree card on one
    repo, a card blocked on a PR, and an after-merge edge whose parent recorded a PR."""
    prg.clear_cache()
    kam._CACHE.clear()
    kam._LAST_CHECKED.clear()
    kbd._ORPHAN_SWEEP_LAST.clear()
    # the real oracle (the batched path is real-oracle only); its gh answers come from the counter
    monkeypatch.setattr(kam, "_default_query", lambda: prg.query_pr)
    monkeypatch.setattr(prg, "repo_exists", lambda slug: True, raising=False)

    def make(conn, n: int):
        _make(conn, n)
        for i in range(n):
            kb.create_task(conn, title=f"wt{i}", assignee="alpha", workspace_kind="worktree",
                           workspace_path=str(anchor))
            gated = kb.create_task(conn, title=f"gated{i}", assignee="alpha")
            kb.block_task(conn, gated, reason=f"waiting on {REPO}#{100 + i}", kind="needs_input")
            parent = kb.create_task(conn, title=f"parent{i}", assignee="alpha")
            child = kb.create_task(conn, title=f"child{i}", assignee="alpha")
            kb.link_tasks(conn, parent, child, kind="after-merge")
            conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) "
                         "VALUES (?,?,?,?,?)", (parent, "alpha", "done", int(time.time()),
                                                json.dumps({"pr_url": f"https://github.com/{REPO}/pull/{200 + i}"})))
        conn.commit()
    return make


def _tick_counted(conn, monkeypatch, n_ready: int, *, sleep: float = 0.0, **kw):
    counter = SpawnCounter(sleep=sleep).install(monkeypatch)
    t0 = time.monotonic()
    res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=n_ready, **kw)
    wall = time.monotonic() - t0
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    return counter, res, wall


# --------------------------------------------------------------------------- 5 (the contract)


def test_tick_child_processes_do_not_grow_with_ready_cards(board, monkeypatch):
    """The guard (item 3): N=2 and N=12 ready cards of every measured kind spawn the same number of children,
    and every spawning call site is allow-listed. Merge base: 36 -> 116 (git rev-parse per worktree card, gh per
    after-merge PR); see the PR body for the table."""
    with kb.connect_closing() as conn:
        board(conn, 2)
        small, res_s, _ = _tick_counted(conn, monkeypatch, 2, **GATEWAY)
    kb_home = Path(kb.kanban_home())
    with kb.connect_closing() as conn:
        # a fresh board with 6x the cards, same caches cold
        for t in conn.execute("SELECT id FROM tasks").fetchall():
            conn.execute("DELETE FROM task_links WHERE parent_id=? OR child_id=?", (t[0], t[0]))
        conn.execute("DELETE FROM task_runs")
        conn.execute("DELETE FROM tasks")
        conn.commit()
        prg.clear_cache()
        kam._CACHE.clear()
        kam._LAST_CHECKED.clear()
        kwt._origin_cache.clear()
        board(conn, 12)
        big, res_b, _ = _tick_counted(conn, monkeypatch, 12, **GATEWAY)
    assert kb_home == Path(kb.kanban_home())
    spawned_small = len(res_s.spawned)
    spawned_big = len(res_b.spawned)
    # Per-spawn children (placement prep) are bounded by the allowance, not by the ready count: subtract them.
    per_spawn = {"kanban_worker_hosts.py:_run_ident_probe", "kanban_worktree_port.py:clone_on_host"}
    fixed_small = small.total - sum(small.sites[s] for s in per_spawn)
    fixed_big = big.total - sum(big.sites[s] for s in per_spawn)
    assert fixed_big == fixed_small, (
        f"tick children grew with ready cards: N=2 -> {dict(small.sites)} ({spawned_small} spawned), "
        f"N=12 -> {dict(big.sites)} ({spawned_big} spawned)")
    unlisted = sorted((set(small.sites) | set(big.sites)) - set(ALLOWED_SPAWN_SITES))
    assert not unlisted, f"new synchronous spawn on the tick path, not on ALLOWED_SPAWN_SITES: {unlisted}"


def test_unlisted_spawn_on_the_tick_path_fails_the_contract(board, monkeypatch):
    """The allow-list has teeth: a new ``subprocess.run`` inside the tick is caught by its call site."""
    def new_probe(*a, **k):
        subprocess.run(["true"], check=False)
        return []

    monkeypatch.setattr(kbd, "reap_worker_zombies", new_probe)
    with kb.connect_closing() as conn:
        board(conn, 1)
        counter, _res, _ = _tick_counted(conn, monkeypatch, 1, **GATEWAY)
    # attributed to the hermes frame that called it, which is not on the list
    assert counter.sites["kanban_db_dispatch.py:_dispatch_once_locked"] == 1, dict(counter.sites)
    assert "kanban_db_dispatch.py:_dispatch_once_locked" not in ALLOWED_SPAWN_SITES


# --------------------------------------------------------------------------- 1 (attribution)


def test_board_tick_reports_steps_and_the_rest_is_small(board, monkeypatch):
    """Every top-level step partitions the board tick: with a 0.05 s cost per child, the steps account for the
    tick wall to within 5 % (the live goal is an unattributed remainder under 5 s)."""
    with kb.connect_closing() as conn:
        board(conn, 3)
        counter, res, wall = _tick_counted(conn, monkeypatch, 3, sleep=0.05)
    top = sum(v for k, v in res.steps_ms.items() if "/" not in k)
    assert counter.total > 0
    assert res.steps_ms["reap/orphan_sweep"] >= 0
    for key in ("pr_gate_prefetch", "after_merge_release", "reap", "reclaim_promote", "route", "decide"):
        assert key in res.steps_ms, (key, sorted(res.steps_ms))
    assert top <= wall * 1000.0 + 1.0
    assert wall * 1000.0 - top < max(50.0, 0.05 * wall * 1000.0), (wall, top, res.steps_ms)


def test_step_scope_nests_and_records_unattributed():
    from hermes_cli import kanban_tick_steps as kts
    with kts.scope() as outer:
        with kts.step("a"):
            time.sleep(0.01)
        with kts.scope() as inner:
            with kts.step("b/x"):
                pass
            kts.add("b", 5.0)
        assert inner["b"] == 5.0
    assert outer["b"] == 5.0 and outer["a"] >= 10.0 and "b/x" in outer
    rec = kts.record(outer, wall_ms=outer["a"] + 5.0 + 7.0)
    assert rec["unattributed"] == pytest.approx(7.0, abs=0.01)
    assert not kts.active()


def test_tick_memo_is_per_scope_and_never_caches_an_exception():
    from hermes_cli import kanban_tick_steps as kts
    calls = []
    assert kts.tick_memo("k", lambda: calls.append(1) or 1) == 1
    assert kts.tick_memo("k", lambda: calls.append(1) or 1) == 1
    assert len(calls) == 2  # outside a tick: never cached
    with kts.scope():
        for _ in range(3):
            kts.tick_memo("k", lambda: calls.append(1) or 1)
        with pytest.raises(RuntimeError):
            kts.tick_memo("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert kts.tick_memo("boom", lambda: 7) == 7
        with kts.scope():
            kts.tick_memo("inner", lambda: calls.append(2) or 2, inner=True)
            kts.tick_memo("inner", lambda: calls.append(2) or 2, inner=True)
        with kts.scope():
            kts.tick_memo("inner", lambda: calls.append(2) or 2, inner=True)
    assert calls.count(1) == 3 and calls.count(2) == 2


def test_gate_series_line_carries_steps_ms():
    from types import SimpleNamespace

    from gateway import kanban_gate_series as series
    rec = series.tick_record(
        ts=1.0, gate=SimpleNamespace(), tick=SimpleNamespace(), builder=SimpleNamespace(), allowance=None,
        pool_budget=None, spawn_paused=None, local_spawned=0, results=[], tick_ms_cpu=1.0, tick_ms_wall=100.0,
        boards=1, queries=0, steps={"route": 60.0, "route/route_rows": 50.0, "demand_scan": 10.0})
    assert rec["steps_ms"] == {"demand_scan": 10.0, "route": 60.0, "route/route_rows": 50.0,
                               "unattributed": 30.0}
    assert "steps_ms" in series.FIELDS


# --------------------------------------------------------------------------- 2 (orphan sweep off the tick)


@pytest.fixture
def sweep_conn(kanban_home, monkeypatch):
    kbd._ORPHAN_SWEEP_LAST.clear()
    kbd._ORPHAN_SWEEP_JOBS.clear()
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "reap_exited_worker_leftovers", lambda c, pids: [])
    clock = [1000.0]
    monkeypatch.setattr(kbd, "_orphan_sweep_clock", lambda: clock[0], raising=False)
    with kb.connect_closing() as conn:
        yield conn, clock


def _drain_sweeps(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(f.done() for f in list(kbd._ORPHAN_SWEEP_JOBS.values())):
            return
        time.sleep(0.01)
    raise AssertionError("background orphan sweep did not finish")


def test_gateway_tick_never_runs_the_orphan_sweep_inline(sweep_conn, monkeypatch):
    conn, _clock = sweep_conn
    ticker = threading.get_ident()
    threads = []
    release = threading.Event()

    def sweep(c, board=None):
        threads.append(threading.get_ident())
        release.wait(60)
        return {"t_aaaaaaaa": [{"pid": 4242}]}

    monkeypatch.setattr(kb, "sweep_terminal_workspace_orphans", sweep)
    try:
        first = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
        # The sweep cannot finish until `release` is set, so a tick that waited on it would return with
        # it done (or never return). Judged on the sweep's state, not a wall-clock bound (t_7c61c59d:
        # a loaded CI runner took 4.65 s for a tick that never waited).
        jobs = list(kbd._ORPHAN_SWEEP_JOBS.values())
        assert jobs and not any(f.done() for f in jobs), "the tick waited on the sweep"
        assert first.orphans_reaped == {}
    finally:
        release.set()
    _drain_sweeps()
    assert threads and ticker not in threads
    second = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
    assert second.orphans_reaped == {"t_aaaaaaaa": [{"pid": 4242}]}, "the finished sweep's result was not booked"


def test_background_sweep_runs_at_most_once_per_cadence_window(sweep_conn, monkeypatch):
    conn, clock = sweep_conn
    runs = []
    monkeypatch.setattr(kb, "sweep_terminal_workspace_orphans", lambda c, board=None: runs.append(board) or {})
    for _ in range(4):
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
        _drain_sweeps()
        clock[0] += 60
    assert runs == ["default"]
    clock[0] += kbd.ORPHAN_SWEEP_INTERVAL_SECONDS
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
    _drain_sweeps()
    assert runs == ["default", "default"]


def test_a_sweep_that_raises_does_not_abort_the_tick(sweep_conn, monkeypatch):
    conn, _clock = sweep_conn

    def boom(c, board=None):
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(kb, "sweep_terminal_workspace_orphans", boom)
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
    _drain_sweeps()
    tid = kb.create_task(conn, title="still dispatches", assignee="alpha")
    spawned = []
    res = kbd.dispatch_once(conn, spawn_fn=lambda task, ws, **k: spawned.append(task.id) or 4242,
                            board="default", defer_maintenance=True)
    assert spawned == [tid] and res.orphans_reaped == {}


def test_no_overlapping_background_sweeps(sweep_conn, monkeypatch):
    conn, clock = sweep_conn
    gate = threading.Event()
    runs = []

    def slow(c, board=None):
        runs.append(1)
        gate.wait(5)
        return {}

    monkeypatch.setattr(kb, "sweep_terminal_workspace_orphans", slow)
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
    clock[0] += kbd.ORPHAN_SWEEP_INTERVAL_SECONDS * 3   # due again while the first still runs
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
    gate.set()
    _drain_sweeps()
    assert runs == [1]


def test_a_failed_thread_start_leaves_no_job_and_the_next_due_tick_sweeps(sweep_conn, monkeypatch):
    # Prism f83a6a86a55f: a Future registered before a start() that raised never finished and blocked every
    # later sweep for the (home, board) until restart.
    conn, clock = sweep_conn
    runs = []
    monkeypatch.setattr(kb, "sweep_terminal_workspace_orphans", lambda c, board=None: runs.append(board) or {})
    real_start = threading.Thread.start
    failed = []

    def start(self):
        if self.name == "kanban-orphan-sweep" and not failed:
            failed.append(1)
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
    assert failed == [1]
    assert kbd._ORPHAN_SWEEP_JOBS == {}, "a job stayed registered after its thread failed to start"
    clock[0] += kbd.ORPHAN_SWEEP_INTERVAL_SECONDS
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default", defer_maintenance=True)
    _drain_sweeps()
    assert runs == ["default"]


# --------------------------------------------------------------------------- 3 (after-merge, batched)


class FakeGh:
    """``gh`` at ``subprocess.run`` for the after-merge reads: REST per PR and GraphQL per repo."""

    def __init__(self, states):
        self.states = states
        self.calls = []
        self.real = subprocess.run

    def __call__(self, args, *a, **kw):
        if not (isinstance(args, list) and args[:2] == ["gh", "api"]):
            return self.real(args, *a, **kw)
        self.calls.append(args[2])
        if args[2] == "graphql":
            import re
            fields = dict(x.split("=", 1) for x in args[3:] if not x.startswith("-"))
            nodes = {f"p{n}": ({"state": self.states[int(n)], "mergedAt":
                                "2099-01-01T00:00:00Z" if self.states[int(n)] == "MERGED" else None,
                                "mergeCommit": None} if int(n) in self.states else None)
                     for n, _ in re.findall(r"p(\d+): pullRequest\(number: (\d+)\)", fields["query"])}
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"repository": nodes}}), "")
        n = int(args[2].rsplit("/", 1)[-1])
        if n not in self.states:
            return subprocess.CompletedProcess(args, 1, "", "")
        st = self.states[n]
        return subprocess.CompletedProcess(args, 0, json.dumps({
            "state": "open" if st == "OPEN" else "closed",
            "merged_at": "2099-01-01T00:00:00Z" if st == "MERGED" else None}), "")


def _after_merge_board(conn, states):
    pairs = []
    for n in sorted(states):
        parent = kb.create_task(conn, title=f"p{n}", assignee="alpha")
        child = kb.create_task(conn, title=f"c{n}", assignee="alpha")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, metadata) VALUES (?,?,?,?,?)",
                     (parent, "alpha", "done", 1, json.dumps({"pr_url": f"https://github.com/{REPO}/pull/{n}"})))
        pairs.append((parent, child, n))
    conn.commit()
    return pairs


@pytest.mark.parametrize("mode", ["batched", "per_pr"])
def test_after_merge_verdicts_unchanged_and_reads_batched(kanban_home, monkeypatch, mode):
    """Same fixtures, same releases and escalations, batched or per PR; the real oracle forks ONE gh per repo."""
    kam._CACHE.clear()
    kam._LAST_CHECKED.clear()
    states = {11: "MERGED", 12: "OPEN", 13: "CLOSED", 14: "MERGED"}   # 15: unreadable
    fake = FakeGh(states)
    monkeypatch.setattr(prg.subprocess, "run", fake)
    with kb.connect_closing() as conn:
        pairs = _after_merge_board(conn, {**states, 15: None})
        if mode == "per_pr":
            # the old shape: a non-identical oracle (wrapper) takes the per-PR path; sandbox it
            monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
            released = kam.release_merged_edges(conn, query_fn=lambda r, n: prg.query_pr(r, n))
        else:
            released = kam.release_merged_edges(conn, query_fn=prg.query_pr)
        escalated = {r[0] for r in conn.execute(
            "SELECT task_id FROM task_events WHERE kind='after_merge_prerequisite_closed'")}
    by_n = {n: (p, c) for p, c, n in pairs}
    assert sorted(released) == sorted([by_n[11], by_n[14]])
    assert escalated == {by_n[13][1]}
    if mode == "batched":
        assert fake.calls == ["graphql"], fake.calls
    else:
        assert len(fake.calls) == 5


def test_after_merge_batch_spends_the_same_lookup_budget(kanban_home, monkeypatch):
    """The batch fetches only the refs the per-ref loop would read within ``max_lookups``."""
    kam._CACHE.clear()
    kam._LAST_CHECKED.clear()
    states = {n: "OPEN" for n in range(21, 36)}
    fake = FakeGh(states)
    monkeypatch.setattr(prg.subprocess, "run", fake)
    asked = []
    real_chunk = prg._query_pr_chunk
    monkeypatch.setattr(prg, "_query_pr_chunk", lambda repo, nums: asked.extend(nums) or real_chunk(repo, nums))
    with kb.connect_closing() as conn:
        _after_merge_board(conn, states)
        kam.release_merged_edges(conn, query_fn=prg.query_pr, max_lookups=kam.MAX_LOOKUPS_PER_TICK)
    assert len(asked) == kam.MAX_LOOKUPS_PER_TICK


# --------------------------------------------------------------------------- 4 (worktree routing)


def test_worktree_routing_verdicts_unchanged_with_one_ref_read_per_repo(board, anchor, monkeypatch):
    """N worktree cards on one repo: the same route as card-by-card reads, and one for-each-ref per tick."""
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i", cwd=anchor)
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=anchor)
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"wt{i}", assignee="alpha", workspace_kind="worktree",
                              workspace_path=str(anchor)) for i in range(6)]
        rows = [conn.execute("SELECT * FROM tasks WHERE id=?", (t,)).fetchone() for t in ids]
        plain = [kbd._route_row(conn, r, _plan(free=2), worktree_out={}) for r in rows]
        counter = SpawnCounter().install(monkeypatch)
        from hermes_cli import kanban_tick_steps as kts
        with kts.scope():
            out: dict = {}
            memo = [kbd._route_row(conn, r, _plan(free=2), worktree_out=out) for r in rows]
        monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    assert memo == plain
    assert counter.sites["kanban_worktree_port.py:_read"] == 1
    assert counter.sites["kanban_worktree_port.py:_git"] == 0
    assert counter.sites["kanban_db.py:_git_out"] <= 3   # toplevel + git-dir + common-dir, once


def test_background_sweep_runs_under_the_tick_threads_home_context(sweep_conn, monkeypatch, tmp_path):
    """Prism 9bb17d5fb49b: a ContextVar home override (a profile-scoped gateway tick) reaches the sweep thread,
    so the sweep connects to and reaps the board the tick was running for, not the process default."""
    import hermes_constants as vc

    seen = []

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(kb, "connect", lambda board=None: seen.append(("connect", vc.get_hermes_home_override()))
                        or _Conn())
    monkeypatch.setattr(kb, "sweep_terminal_workspace_orphans",
                        lambda c, board=None: seen.append(("sweep", vc.get_hermes_home_override())) or {})
    other = str(tmp_path / "profile-home")
    tok = vc.set_hermes_home_override(other)
    try:
        kbd._orphan_sweep_off_tick("default")
    finally:
        vc.reset_hermes_home_override(tok)
    _drain_sweeps()
    assert seen == [("connect", other), ("sweep", other)], seen


def test_worktree_port_rereads_a_branch_tip_that_moved_inside_the_tick(anchor, monkeypatch):
    """Prism ce53b51a0bc2: an own clean worktree gains an unpushed commit after the repo's refs were read this
    tick. The port must see the new tip (push it first), never port origin's stale tip with push=None."""
    from hermes_cli import kanban_tick_steps as kts

    def commit(cwd, msg):
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", msg, cwd=cwd)

    tid = "t_bbbbbbbb"
    commit(anchor, "A")
    _git("branch", f"wt/{tid}", cwd=anchor)
    _git("update-ref", f"refs/remotes/origin/wt/{tid}", "HEAD", cwd=anchor)
    tree = anchor / ".worktrees" / tid
    _git("worktree", "add", "-q", str(tree), f"wt/{tid}", cwd=anchor)
    kw = dict(allow_studio_tree=True, push_ok=True)
    with kts.scope():
        top, push = kwt.assess(tid, str(tree), None, **kw)
        assert top is not None and push is None, (top, push)   # in step with origin: the table is warm
        commit(tree, "B")
        top, push = kwt.assess(tid, str(tree), None, **kw)
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    assert push is not None and push[3] == head, push



def test_worktree_port_rereads_a_cached_absence_once_an_own_tree_exists(anchor, monkeypatch):
    """Prism 4ceddf92a18f: the table was read before the card's branch and own worktree existed. Once that tree
    exists with an unpushed commit, the port must see the branch (push it), never port origin with push=None."""
    from hermes_cli import kanban_tick_steps as kts

    def commit(cwd, msg):
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", msg, cwd=cwd)

    commit(anchor, "A")
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=anchor)
    tid = "t_bbbbbbbb"
    kw = dict(allow_studio_tree=True, push_ok=True)
    with kts.scope():
        kwt.assess("t_aaaaaaaa", str(anchor), None, **kw)           # warms the repo's ref table
        tree = anchor / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(tree), cwd=anchor)
        commit(tree, "B")
        top, push = kwt.assess(tid, str(tree), None, **kw)
    head = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    assert push is not None and push[3] == head, (top, push)

def test_a_running_background_sweep_never_holds_interpreter_exit(tmp_path):
    """Gateway shutdown (SIGTERM, then SIGKILL after the grace): a sweep still running must not hold the exit. A
    ThreadPoolExecutor worker is joined at interpreter shutdown, and the sweep's cwd probe has no total bound
    (15 s start + 2 s per hung pid), so the sweep runs on a daemon thread instead."""
    root = Path(__file__).resolve().parents[2]
    script = (
        "import sys, time, threading\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd\n"
        "started = threading.Event()\n"
        "kb.connect = lambda board=None: type('C', (), {'close': lambda self: None})()\n"
        "kb.sweep_terminal_workspace_orphans = lambda c, board=None: (started.set(), time.sleep(25))\n"
        "kbd._orphan_sweep_due = lambda board, now=None: True\n"
        "kb.kanban_home = lambda: '/nonexistent-home'\n"
        "kbd._orphan_sweep_off_tick('default')\n"
        "assert started.wait(10)\n"
    )
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=45)
    took = time.monotonic() - t0
    assert proc.returncode == 0, proc.stderr
    assert took < 15, f"interpreter exit waited {took:.1f}s on the running sweep"

