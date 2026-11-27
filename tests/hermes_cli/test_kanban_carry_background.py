"""Workspace carry with no size cap, off the dispatcher tick (t_f3a411c2; Ace 2026-11-12 09:25 "no cap").

The pool host is a LOOPBACK here: every ``ssh <target> <script>`` the carry issues runs as ``bash -c <script>`` and
every rsync runs for real into a local directory, with the workspace path rewritten from the Studio root to a
``pool`` root. So the copies, the staging dir, ``df``, ``du``, the finalize ``mv`` and an rsync killed mid-copy are
real; only the network hop is not.

The 3 GiB / 120,000-entry fixture is opt-in (``CARRY_BIG_FIXTURE=1``): CI never writes 3 GiB.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_worker_hosts as kwh

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_last_resort import _write_policy

try:  # absent on a tree before t_f3a411c2: the dispatch assertions below are then the red
    from hermes_cli import kanban_carry_background as kcb
except ImportError:  # pragma: no cover
    kcb = None


class Loopback:
    """A pool host that is this machine under ``pool``: ssh scripts run in bash, rsync copies locally."""

    def __init__(self, studio: Path, pool: Path, fake_bin: Path = None):
        self.studio, self.pool, self.fake_bin = str(studio), str(pool), fake_bin
        self.calls = []
        self.kill_rsync_after_files = None  # interrupt: kill the rsync once this many files reached the staging dir
        self.rsync_rc = []

    def _map(self, s: str) -> str:
        return s.replace(self.studio, self.pool)

    def __call__(self, argv, **kw):
        kw.pop("stdin", None)
        if "input" not in kw:
            kw["stdin"] = subprocess.DEVNULL
        env = dict(os.environ)
        if self.fake_bin is not None:
            env["PATH"] = f"{self.fake_bin}:{env['PATH']}"
        if argv[0] == "ssh":
            self.calls.append(("ssh", self._map(argv[-1])))
            return subprocess.run(["bash", "-c", self._map(argv[-1])], env=env, **kw)
        assert argv[0] == "rsync", argv
        i = argv.index("-e")
        local = argv[:i] + argv[i + 2:]
        local[-1] = self._map(local[-1].split(":", 1)[1])
        self.calls.append(("rsync", local))
        if self.kill_rsync_after_files is None:
            proc = subprocess.run(local, env=env, **kw)
            self.rsync_rc.append(proc.returncode)
            return proc
        dest = Path(local[-1])
        # throttled so the kill lands mid-copy (an option: before the "--" that ends them)
        p = subprocess.Popen([local[0], "--bwlimit=2000", *local[1:]], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        deadline = time.monotonic() + 60
        while p.poll() is None and time.monotonic() < deadline:
            if dest.is_dir() and sum(len(f) for _r, _d, f in os.walk(dest)) >= self.kill_rsync_after_files:
                p.kill()  # the interrupt: SIGKILL mid-copy, as a reboot or OOM would
                break
            time.sleep(0.02)
        out, err = p.communicate()
        self.rsync_rc.append(p.returncode)
        self.kill_rsync_after_files = None
        return subprocess.CompletedProcess(local, p.returncode, out, err)


@pytest.fixture
def board(kanban_home, monkeypatch):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    _write_policy(fleet)
    (kanban_home / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: false\n")
    kwh._carry_scan_cache.clear()
    kwh._carry_waiting.clear()
    if kcb is not None:
        kcb.reset()
    # the in-tick copy must never run on this path: a call is a test failure
    monkeypatch.setattr(kwh, "carry_workspace", lambda *a, **k: pytest.fail("in-tick carry_workspace ran"))
    yield kanban_home
    if kcb is not None:
        kcb.reset()


@pytest.fixture
def loop(board, tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    pool.mkdir()
    lb = Loopback(board.parent, pool)
    if kcb is not None:
        monkeypatch.setattr(kcb, "_run", lb)
        monkeypatch.setattr(kcb, "RETRY_S", 0.0)
    # prepare_remote_workspace's default runner was bound at import; route it through the loopback too
    real_prep = kwh.prepare_remote_workspace
    monkeypatch.setattr(kwh, "prepare_remote_workspace",
                        lambda host, ws, runner=None, **kw: real_prep(host, ws, lb, **kw))
    return lb


def _card(conn, files=None):
    tid = kb.create_task(conn, title="lf", assignee="alpha")
    path = Path(kbw.resolve_workspace(kb.get_task(conn, tid)))
    path.mkdir(parents=True, exist_ok=True)
    for rel, data in (files or {"notes.md": b"prior run's work\n"}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(data)
    kbw.set_workspace_path(conn, tid, str(path))
    return tid, path


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _comments(conn, tid):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,))]


def _tick_full(conn, **kw):
    return _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu", **kw)


def _wait_done(tid, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with kcb._lock:
            hit = kcb._jobs.get(tid)
        if hit is None or hit[0].future.done():
            return
        time.sleep(0.05)
    raise AssertionError("background carry did not finish")


def _tree(root: Path):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            out[str(p.relative_to(root))] = p.stat().st_size
    return out


# -- the carry leaves the tick -------------------------------------------------------------------------------------

def test_tick_starts_the_carry_and_returns_while_it_copies(board, loop, monkeypatch):
    """The tick only submits the copy; a copy that blocks does not block the tick. The card waits carry_pending."""
    import threading
    gate = threading.Event()
    real = kcb.run

    def slow(job, runner=None):
        gate.wait(30)
        return real(job, runner)
    monkeypatch.setattr(kcb, "run", slow)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        t0 = time.monotonic()
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        tick_s = time.monotonic() - t0
        assert spawned == [] and res.placement_waits[tid] == "carry_pending:ace-ai", res.placement_waits
        assert _events(conn, tid, kcb.STARTED_EVENT) == [{"host": "ace-ai", "path": str(path), "background": True}]
        res, spawned = _tick_full(conn, defer_pool_clone=True)  # still copying: waits again, no second job
        assert spawned == [] and res.placement_waits[tid] == "carry_pending:ace-ai"
        assert len(_events(conn, tid, kcb.STARTED_EVENT)) == 1
        gate.set()
        _wait_done(tid)
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        ev = _events(conn, tid, kwh.CARRIED_EVENT)
    assert tick_s < 5.0, tick_s
    assert (tid, "ace-ai") in spawned
    assert ev[-1]["host"] == "ace-ai" and ev[-1]["path"] == str(path) and ev[-1]["background"]["entries"] == 1
    assert (Path(loop._map(str(path))) / "notes.md").read_bytes() == b"prior run's work\n"
    assert not Path(loop._map(kcb.staging_path(str(path)))).exists()
    assert (path / "notes.md").exists()  # the Studio copy is untouched


def test_over_the_old_caps_is_carried(board, loop):
    """25,000 entries / 1.1 GB sparse would have waited under CARRY_MAX_ENTRIES; it is carried now."""
    files = {f"d{i // 1000}/f{i}": b"x" for i in range(kwh.CARRY_MAX_ENTRIES + 5000)}
    with kb.connect_closing() as conn:
        tid, path = _card(conn, files)
        assert not kwh.carry_size_ok(str(path))  # the old rule's verdict on this dir
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        ev = _events(conn, tid, kwh.CARRIED_EVENT)[-1]
    assert (tid, "ace-ai") in spawned
    assert ev["background"]["entries"] > kwh.CARRY_MAX_ENTRIES
    assert _tree(Path(loop._map(str(path)))) == _tree(path)


def test_one_shot_tick_carries_inline(board, loop):
    """A CLI tick (no defer) runs the same carry in the tick, as the pool clone does."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _res, spawned = _tick_full(conn)
    assert (tid, "ace-ai") in spawned
    assert (Path(loop._map(str(path))) / "notes.md").exists()


# -- an interrupted copy -------------------------------------------------------------------------------------------

def test_interrupted_copy_is_resumable_named_and_resumed(board, loop):
    files = {f"d{i // 100}/f{i}": os.urandom(8192) for i in range(1500)}
    with kb.connect_closing() as conn:
        tid, path = _card(conn, files)
        loop.kill_rsync_after_files = 50
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        assert spawned == [] and res.placement_waits[tid].startswith("carry_failed:ace-ai: rsync rc=")
        failed = _events(conn, tid, kcb.FAILED_EVENT)
        assert failed[-1]["state"] == "resumable" and failed[-1]["phase"] == "copy"
        assert any("failed (resumable)" in c and ".carry-" in c for c in _comments(conn, tid))
        staging = Path(loop._map(kcb.staging_path(str(path))))
        partial = _tree(staging)
        assert 0 < len(partial) < len(files)
        # nothing half-carried at the workspace path itself: the card never ran against a partial copy
        assert _tree(Path(loop._map(str(path)))) == {}
        assert not _events(conn, tid, kwh.CARRIED_EVENT)
        # next attempt (RETRY_S = 0): resumes into the same staging dir, then finalizes
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        ev = _events(conn, tid, kwh.CARRIED_EVENT)
    assert loop.rsync_rc[0] != 0 and loop.rsync_rc[-1] == 0
    assert (tid, "ace-ai") in spawned and ev
    assert _tree(Path(loop._map(str(path)))) == _tree(path)
    assert not staging.exists()


def test_a_dispatcher_restart_mid_carry_cleans_the_other_host(board, loop):
    """A started carry with no live job (the gateway restarted) placed on another host removes the staging dir it
    may have left on the first one."""
    from tests.hermes_cli.test_kanban_worker_pool import _host
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        kb._append_event(conn, tid, kcb.STARTED_EVENT, {"host": "ace-media", "path": str(path), "background": True})
        conn.commit()
        left = Path(loop._map(kcb.staging_path(str(path))))
        left.mkdir(parents=True)
        (left / "half").write_text("x")
        plan = _plan(free=2, hosts=[_host("ace-ai"), _host("ace-media", priority=9)])
        stale = kcb.stale_host(conn, tid, str(path), plan.hosts, "ace-ai")
        assert stale is not None and stale.name == "ace-media"
        job = kcb.Job(tid, plan.hosts["ace-ai"], str(path), stale)
        kcb.run(job)
    # loopback: both hosts are the same directory, so the cleanup ran before this carry's copy
    assert job.stale_cleaned is True
    assert not (Path(loop._map(kcb.staging_path(str(path)))) / "half").exists()


# -- no room on the destination ------------------------------------------------------------------------------------

def test_no_room_refuses_before_copying_naming_host_and_both_numbers(board, loop, tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "df").write_text("#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted'\n"
                             "echo '/dev/x 100 99 1 99% /'\n")
    (fake / "df").chmod(0o755)
    loop.fake_bin = fake
    with kb.connect_closing() as conn:
        tid, path = _card(conn, {"big": b"y" * 50_000})
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        failed = _events(conn, tid, kcb.FAILED_EVENT)[-1]
        comments = _comments(conn, tid)
    assert spawned == []
    assert res.placement_waits[tid].startswith("carry_no_room:ace-ai: No room on ace-ai: need ")
    assert "need 50" in failed["reason"] and "free 1024" in failed["reason"], failed
    assert failed["state"] == "clean"
    assert any("No room on ace-ai: need " in c and "nothing of this card on the host" in c for c in comments)
    assert not any(c[0] == "rsync" for c in loop.calls)  # nothing copied
    assert not Path(loop._map(kcb.staging_path(str(path)))).exists()


# -- what still keeps the files home --------------------------------------------------------------------------------

def test_a_checkout_whose_git_dir_does_not_travel_is_refused_and_keeps_the_capped_rule(board, loop, tmp_path,
                                                                                         monkeypatch):
    capped = []
    monkeypatch.setattr(kwh, "carry_workspace", lambda host, ws, runner=None, **kw: capped.append(ws))
    outside = tmp_path / "outside.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        (path / "wt").mkdir()
        (path / "wt" / ".git").write_text(f"gitdir: {outside}\n")
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        _tick_full(conn, defer_pool_clone=True)
        failed = _events(conn, tid, kcb.FAILED_EVENT)[-1]
        assert failed["state"] == "refused"
        assert kcb.refused(conn, tid, str(path))
        assert capped == []  # nothing copied by the refused attempt
        _res, spawned = _tick_full(conn, defer_pool_clone=True)  # now the capped rule decides, as before
        started = _events(conn, tid, kcb.STARTED_EVENT)
    assert len(started) == 1 and capped == [str(path)]  # no second background attempt; the in-tick rule ran
    assert not Path(loop._map(kcb.staging_path(str(path)))).exists()


def test_a_checkout_rooted_at_the_workspace_whose_git_dir_does_not_travel_is_refused(board, loop, tmp_path):
    """Prism b68c58398adb: the git-state check runs from the workspace root, so a linked checkout AT the root counts."""
    outside = tmp_path / "outside.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        (path / ".git").write_text(f"gitdir: {outside}\n")
        job = kcb.Job(tid, _plan(free=2).hosts["ace-ai"], str(path))
        with pytest.raises(kcb.CarryRefused):
            kcb.run(job)
    assert not any(c[0] == "rsync" for c in loop.calls)


def test_the_carry_lock_is_held_from_the_start_until_the_finalize(board, loop, tmp_path):
    """Prism 18996b99e57f / b198d365c020: a finished copy keeps its (host, path) lock until its staging dir is
    consumed, so no other dispatcher can start an rsync into a staging dir being moved."""
    lock_dir = tmp_path / "locks"
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        host = _plan(free=2).hosts["ace-ai"]
        state, job = kcb.poll(tid, host, str(path), lock_dir)
        assert state == "started"
        _wait_done(tid)
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "done"
        lp = kcb.lock_path(lock_dir, host.name, str(path))
        assert kcb._try_lock(lp) is None          # another dispatcher: still locked after the copy finished
        assert kcb.take(tid, host, str(path)) is job
        assert kcb._try_lock(lp) is None          # still locked across the finalize
        kcb.finalize(host, str(path), runner=loop)
        kcb.release(job)
        fd = kcb._try_lock(lp)
    assert fd is not None
    kcb._unlock(fd)


def test_a_sync_carry_whose_claim_is_skipped_does_not_strand_its_lock(board, loop, tmp_path):
    """Prism eba22256a789: a one-shot carry that finished but whose claim never ran (claim_task None, PauseHeld, a
    route failure) is kept in the registry, not only in the tick's dict: the next carry of that card in the same
    process reuses its lock, and a finalize still releases it."""
    lock_dir = tmp_path / "locks"
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        host = _plan(free=2).hosts["ace-ai"]
        lp = kcb.lock_path(lock_dir, host.name, str(path))
        assert kcb.run_sync(kcb.Job(tid, host, str(path), None, lock_dir), lock_dir, runner=loop)
        # the tick that ran it exits without the claim: its tick-local job is dropped
        job2 = kcb.Job(tid, host, str(path), None, lock_dir)
        assert kcb.run_sync(job2, lock_dir, runner=loop)  # not stranded: the same process carries it again
        assert kcb.take(tid, host, str(path)) is job2
        kcb.finalize(host, str(path), runner=loop)
        kcb.release(job2)
        fd = kcb._try_lock(lp)
    assert fd is not None
    kcb._unlock(fd)


def test_a_kept_sync_carry_is_refreshed_in_a_process_that_never_started_a_background_one(
        board, loop, tmp_path, monkeypatch):
    """Prism 64edba28c538: a one-shot carry kept in the registry and then polled once older than FRESH_S is
    refreshed on the carry pool, which this process may never have created (no background carry ran yet)."""
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(kcb, "_pool", None)
    monkeypatch.setattr(kcb, "FRESH_S", 0.0)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        host = _plan(free=2).hosts["ace-ai"]
        job = kcb.Job(tid, host, str(path), None, lock_dir)
        assert kcb.run_sync(job, lock_dir, runner=loop)   # its own tick handed it to a claim, which was skipped
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "running"
        _wait_done(tid)
        monkeypatch.setattr(kcb, "FRESH_S", 3600.0)
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "done"
        assert kcb.take(tid, host, str(path)) is job
        kcb.finalize(host, str(path), runner=loop)
        kcb.release(job)


def test_a_finished_copy_that_waited_is_refreshed_before_the_claim(board, loop, tmp_path, monkeypatch):
    """Prism 38c86a910163: a Studio edit made after the copy finished reaches the pool host when the finished copy
    is older than FRESH_S: it is re-synced off the tick, never claimed stale."""
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(kcb, "FRESH_S", 0.0)
    with kb.connect_closing() as conn:
        tid, path = _card(conn, {"notes.md": b"v1\n"})
        host = _plan(free=2).hosts["ace-ai"]
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "started"
        _wait_done(tid)
        (path / "notes.md").write_bytes(b"v2\n")
        later = time.time() + 5
        os.utime(path / "notes.md", (later, later))   # an edit after the copy: rsync's quick check sees the mtime
        # the first look at a finished copy hands it to the claim whatever its age (operator 16:40 item 4); a claim
        # that passed it over (no slot) lets the next look refresh it
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "done"
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "running"   # the refresh, off the tick
        _wait_done(tid)
        monkeypatch.setattr(kcb, "FRESH_S", 3600.0)
        state, job = kcb.poll(tid, host, str(path), lock_dir)
        assert state == "done"
        kcb.take(tid, host, str(path))
        kcb.finalize(host, str(path), runner=loop)
        kcb.release(job)
    assert (Path(loop._map(str(path))) / "notes.md").read_bytes() == b"v2\n"


def test_stale_staging_is_left_alone_while_its_host_lock_is_held(board, loop, tmp_path):
    """Prism 4d42b4210a5a: the stale host's staging dir is removed only under that host's carry lock."""
    from tests.hermes_cli.test_kanban_worker_pool import _host
    lock_dir = tmp_path / "locks"
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        plan = _plan(free=2, hosts=[_host("ace-ai"), _host("ace-media", priority=9)])
        left = Path(loop._map(kcb.staging_path(str(path))))
        left.mkdir(parents=True)
        (left / "half").write_text("x")
        held = kcb._try_lock(kcb.lock_path(lock_dir, "ace-media", str(path)))   # ace-media's carry is live
        try:
            job = kcb.Job(tid, plan.hosts["ace-ai"], str(path), plan.hosts["ace-media"], lock_dir)
            removes = []
            real = kcb.remove_staging
            kcb.remove_staging, _r = (lambda h, ws, runner=None: removes.append(h.name) or real(h, ws, runner)), real
            try:
                kcb.run(job)
            finally:
                kcb.remove_staging = _r
        finally:
            kcb._unlock(held)
    assert job.stale_cleaned is False and removes == []


def test_finalize_into_a_nonempty_workspace_never_writes_through_a_symlink(board, loop, tmp_path):
    """Prism 0e52f77edd32: a destination file symlink is replaced, as the in-tick rsync carry did."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
    host = _plan(free=2).hosts["ace-ai"]
    dest = Path(loop._map(str(path)))
    dest.mkdir(parents=True)
    victim = tmp_path / "important.txt"
    victim.write_text("keep me\n")
    (dest / "notes.md").symlink_to(victim)
    (dest / "other").write_text("left\n")
    staging = Path(loop._map(kcb.staging_path(str(path))))
    staging.mkdir(parents=True)
    (staging / "notes.md").write_text("new\n")
    kcb.finalize(host, str(path), runner=loop)
    assert victim.read_text() == "keep me\n"
    assert not (dest / "notes.md").is_symlink() and (dest / "notes.md").read_text() == "new\n"
    assert (dest / "other").read_text() == "left\n" and not staging.exists()


def test_kill_switch_restores_the_capped_in_tick_carry(board, loop, monkeypatch):
    (board / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: false\n  placement:\n"
                                       "    carry_caps: true\n")
    assert kcb.caps_on()
    calls = []
    monkeypatch.setattr(kwh, "carry_workspace", lambda host, ws, runner=None, **kw: calls.append((host.name, ws)))
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        assert not _events(conn, tid, kcb.STARTED_EVENT)
    assert calls == [("ace-ai", str(path))] and (tid, "ace-ai") in spawned
    monkeypatch.setattr(kwh, "carry_size_ok", lambda path, **_kw: False)
    with kb.connect_closing() as conn:
        tid2, _ = _card(conn)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
    assert (tid2, "ace-ai") not in spawned  # over the caps: waits, exactly as before


def test_caps_knob_parsing():
    assert kcb.caps_on({}) is False
    assert kcb.caps_on({"placement": {"carry_caps": True}}) is True
    assert kcb.caps_on({"placement": {"carry_caps": "off"}}) is False
    assert kcb.caps_on({"placement": {"carry_caps": "on"}}) is True


# -- checkouts whose git dir does not travel: rebuilt on the pool host (operator 16:40 11-12, item 1) ---------------

def _g(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _origin_with_many_files(tmp_path: Path, n: int) -> Path:
    """A bare origin whose main holds ``n`` tracked files (they never travel: the pool clone gets them). ``tmp_path``
    must lie outside the loopback's Studio root: the loopback rewrites that root in every remote script."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _g("init", "-q", str(seed))
    for i in range(n):
        d = seed / f"src{i // 1000}"
        d.mkdir(exist_ok=True)
        (d / f"f{i}.txt").write_text(f"{i}\n")
    (seed / "tracked.txt").write_text("v1\n")
    (seed / ".gitignore").write_text("*.env\n__pycache__/\n")
    _g("add", "-A", cwd=seed)
    _g("commit", "-qm", "base", cwd=seed)
    origin = tmp_path / "origin.git"
    _g("clone", "-q", "--bare", str(seed), str(origin))
    return origin


def _git_view(repo: Path):
    return (_g("rev-parse", "HEAD", cwd=repo), _g("status", "--porcelain", cwd=repo), _g("stash", "list", cwd=repo),
            _g("branch", "--show-current", cwd=repo))


def _carry_until_claimed(conn, tid, ticks=6):
    waits = []
    for _ in range(ticks):
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        if tid in res.placement_waits:
            waits.append(res.placement_waits[tid])
        if (tid, "ace-ai") in spawned:
            return waits, spawned
        _wait_done(tid)
    raise AssertionError(f"never claimed; waits: {waits}")


@pytest.mark.parametrize("stash_carry", [False, True], ids=["stash_carry_off_default", "stash_carry_on"])
def test_a_linked_worktree_of_a_repo_outside_the_workspace_is_rebuilt_with_its_state(board, loop, tmp_path,
                                                                                       tmp_path_factory, monkeypatch,
                                                                                       stash_carry):
    """Item 1a: over 20,000 entries, a linked worktree whose git dir lives outside the workspace (as every waiting
    card's ./repo does: ~/.hermes/.git/worktrees/repoN), with one unpushed commit, one modified tracked file, one
    untracked file, one ignored file and one stash entry. HEAD, status and branch match on the pool host.

    The stash follows kanban.placement.worktree_stash_carry (operator 18:55 11-12 item 1; PR 253's knob). Off (the
    default): a linked worktree's stash list is the Studio home repo's SHARED list (other sessions' work), so no entry
    rides the bundle, the host's list is empty, and the carried event and a comment say how many stayed. On: the
    lists are equal."""
    monkeypatch.setattr(kwh, "stash_carry_on", lambda kcfg=None: stash_carry)
    origin = _origin_with_many_files(tmp_path_factory.mktemp("origin"), kwh.CARRY_MAX_ENTRIES + 500)
    home = tmp_path / "home"
    _g("clone", "-q", str(origin), str(home))
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        wt = path / "repo"
        _g("worktree", "add", "-q", "-b", "card/x", str(wt), cwd=home)
        assert (wt / ".git").is_file()
        assert kwh.carried_gitdir_loss(str(path), (".",), ()) is not None  # the check that refused before
        (wt / "tracked.txt").write_text("stashed\n")
        _g("stash", "push", "-q", "-m", "a stash", cwd=wt)
        (wt / "new.txt").write_text("committed, not pushed\n")
        _g("add", "new.txt", cwd=wt)
        _g("commit", "-qm", "unpushed", cwd=wt)
        (wt / "tracked.txt").write_text("modified\n")
        (wt / "scratch.txt").write_text("untracked\n")
        (wt / "secret.env").write_text("ignored\n")
        (wt / "staged.txt").write_text("staged\n")
        _g("add", "staged.txt", cwd=wt)
        assert kcb.census(str(path))[1] > kwh.CARRY_MAX_ENTRIES
        before = _git_view(wt)
        assert before[2]                                    # the source has one stash entry
        waits, _spawned = _carry_until_claimed(conn, tid)
        ev = _events(conn, tid, kwh.CARRIED_EVENT)[-1]
        assert not _events(conn, tid, kcb.FAILED_EVENT)
        comments = _comments(conn, tid)
    dest = Path(loop._map(str(wt)))
    assert (dest / ".git").is_dir()                        # a self-contained checkout on the pool host
    after = _git_view(dest)
    assert (after[0], after[1], after[3]) == (before[0], before[1], before[3])   # HEAD, status, branch
    assert (dest / "secret.env").read_text() == "ignored\n" and (dest / "scratch.txt").read_text() == "untracked\n"
    assert _g("diff", "--cached", "--name-only", cwd=dest) == "staged.txt"
    assert all("over the carry" not in w for w in waits), waits   # item 1c
    rebuilt = ev["background"]["rebuilt"]
    assert [r["rel"] for r in rebuilt] == ["repo"]
    if stash_carry:
        assert after[2] == before[2] and rebuilt[0]["stash"] == 1
        assert "stash_left" not in ev and "stash_left" not in rebuilt[0]
    else:
        assert after[2] == ""                               # nothing of the shared list is stored on the host
        assert rebuilt[0]["stash"] == 0 and rebuilt[0]["stash_left"] == 1
        assert ev["stash_left"] == {"repo": 1}
        assert any("1 stash entries of repo stayed on the Studio" in c for c in comments), comments
    # the tracked files travelled through the clone, not rsync: only the changed / untracked / ignored ones did
    assert rebuilt[0]["files"] <= 4
    assert _git_view(wt) == before                          # the Studio checkout is untouched


def test_a_nested_checkout_with_a_detached_head_is_rebuilt_detached(board, loop, tmp_path, tmp_path_factory):
    """Operator 18:55 item 4 (t_e9ccad78's shape): ./repo is a linked worktree on a branch and ./repo/base, nested in
    it, a second linked worktree at a DETACHED HEAD. Each is rebuilt on its own; HEAD, status and branch (empty =
    detached) equal the source, and repo never sends base's files as its own."""
    origin = _origin_with_many_files(tmp_path_factory.mktemp("origin"), 50)
    home = tmp_path / "home"
    _g("clone", "-q", str(origin), str(home))
    base_sha = _g("rev-parse", "HEAD", cwd=home)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        repo = path / "repo"
        _g("worktree", "add", "-q", "-b", "card/n", str(repo), cwd=home)
        (repo / "work.txt").write_text("on the branch\n")
        _g("add", "work.txt", cwd=repo)
        _g("commit", "-qm", "unpushed", cwd=repo)
        base = repo / "base"
        _g("worktree", "add", "-q", "--detach", str(base), base_sha, cwd=home)
        (base / "tracked.txt").write_text("edited in base\n")
        (base / "base-only.txt").write_text("untracked in base\n")
        views = {r: _git_view(r) for r in (repo, base)}
        assert views[base][3] == "" and views[base][0] == base_sha    # detached
        _waits, _spawned = _carry_until_claimed(conn, tid)
        rebuilt = _events(conn, tid, kwh.CARRIED_EVENT)[-1]["background"]["rebuilt"]
    assert [r["rel"] for r in rebuilt] == ["repo", "repo/base"]
    for src, view in views.items():
        dest = Path(loop._map(str(src)))
        got = _git_view(dest)
        assert (got[0], got[1], got[3]) == (view[0], view[1], view[3]), src
    dest_base = Path(loop._map(str(base)))
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=dest_base).returncode == 1   # detached
    assert (dest_base / "base-only.txt").read_text() == "untracked in base\n"
    assert not (Path(loop._map(str(repo))) / "base-only.txt").exists()


def test_a_checkout_with_most_tracked_files_absent_is_rebuilt_with_them_absent(board, loop, tmp_path,
                                                                                tmp_path_factory, capsys,
                                                                                monkeypatch):
    """Operator 18:55 item 4 (t_b3b69464 h1clone's shape: 30,549 deleted paths, 0 files): a linked worktree whose
    tracked files are almost all deleted from its working tree (not from the index). The pool host ends up with the
    clone at the same HEAD and the same paths absent (git status shows the same `` D`` lines). The delete step's wall
    time is printed. ``CARRY_DELETED_N`` sets how many tracked files (default 20,500; 31,000 = the replay's size)."""
    n = int(os.environ.get("CARRY_DELETED_N", kwh.CARRY_MAX_ENTRIES + 500))
    origin = _origin_with_many_files(tmp_path_factory.mktemp("origin"), n)
    home = tmp_path / "home"
    _g("clone", "-q", str(origin), str(home))
    timing = {}
    real = loop.__call__

    def spy(argv, **kw):
        t0 = time.monotonic()
        out = real(argv, **kw)
        if argv[0] == "ssh" and "xargs -0 rm -rf --" in argv[-1]:
            timing["delete_s"] = time.monotonic() - t0
        return out
    monkeypatch.setattr(kcb, "_run", spy)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        wt = path / "repo"
        _g("worktree", "add", "-q", "-b", "card/d", str(wt), cwd=home)
        for d in wt.glob("src*"):
            subprocess.run(["rm", "-rf", "--", str(d)], check=True)
        before = _git_view(wt)
        assert len(_g("ls-files", "-d", cwd=wt).splitlines()) == n   # every tracked src file absent, 0 others
        _waits, _spawned = _carry_until_claimed(conn, tid)
        rebuilt = _events(conn, tid, kwh.CARRIED_EVENT)[-1]["background"]["rebuilt"]
    dest = Path(loop._map(str(wt)))
    got = _git_view(dest)
    assert (got[0], got[1], got[3]) == (before[0], before[1], before[3])
    assert rebuilt[0]["deleted"] == n and rebuilt[0]["files"] == 0
    assert not any(dest.glob("src*/*"))                     # no deleted file came back
    with capsys.disabled():
        print(f"\nDELETED-PATHS CARRY: {n} paths, delete step {timing['delete_s']:.2f} s, "
              f"bundle {rebuilt[0]['bundle_bytes']} B")


def test_a_clone_borrowing_objects_through_alternates_is_rebuilt_self_contained(board, loop, tmp_path,
                                                                                tmp_path_factory):
    """Item 1b: a clone whose objects/info/alternates points outside the workspace (the Studio mirror). On the pool
    host it holds every object itself: ``git fsck --connectivity-only`` passes with the alternates target gone."""
    origin = _origin_with_many_files(tmp_path_factory.mktemp("origin"), kwh.CARRY_MAX_ENTRIES + 500)
    mirror = tmp_path / "mirror.git"
    _g("clone", "-q", "--mirror", str(origin), str(mirror))
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        clone = path / "clone"
        _g("clone", "-q", "--reference", str(mirror), str(origin), str(clone))
        assert (clone / ".git" / "objects" / "info" / "alternates").exists()
        _g("checkout", "-q", "-b", "card/y", cwd=clone)
        (clone / "more.txt").write_text("unpushed\n")
        _g("add", "more.txt", cwd=clone)
        _g("commit", "-qm", "unpushed", cwd=clone)
        (clone / "tracked.txt").write_text("modified\n")
        before = _git_view(clone)
        waits, _spawned = _carry_until_claimed(conn, tid)
    dest = Path(loop._map(str(clone)))
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    mirror.rename(tmp_path / "mirror-gone.git")             # the alternates target absent
    _g("fsck", "--connectivity-only", cwd=dest)
    assert _git_view(dest) == before
    assert all("over the carry" not in w for w in waits), waits   # item 1c


def test_a_checkout_with_no_usable_origin_rides_a_full_bundle(board, loop, tmp_path):
    """A linked worktree of a repo whose origin is neither GitHub nor a local path: every commit rides the bundle."""
    home = tmp_path / "home"
    _g("init", "-q", str(home))
    (home / "a.txt").write_text("a\n")
    _g("add", "a.txt", cwd=home)
    _g("commit", "-qm", "a", cwd=home)
    _g("remote", "add", "origin", "git@gitlab.example:x/y.git", cwd=home)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        wt = path / "wt"
        _g("worktree", "add", "-q", "-b", "card/z", str(wt), cwd=home)
        (wt / "a.txt").write_text("edited\n")
        before = _git_view(wt)
        waits, _spawned = _carry_until_claimed(conn, tid)
        rebuilt = _events(conn, tid, kwh.CARRIED_EVENT)[-1]["background"]["rebuilt"]
    assert rebuilt[0]["full_bundle"] is True
    assert _git_view(Path(loop._map(str(wt)))) == before


def test_one_shot_tick_never_copies_past_the_old_caps_under_the_tick_lock(board, loop):
    """Item 2: a one-shot tick (CLI dispatch, admit) holds the board tick lock throughout, so a dir over the old caps
    is not copied there: the card waits carry_pending, nothing is copied and no lock is taken; the gateway's tick
    carries it."""
    files = {f"d{i // 1000}/f{i}": b"x" for i in range(kwh.CARRY_MAX_ENTRIES + 10)}
    with kb.connect_closing() as conn:
        tid, path = _card(conn, files)
        res, spawned = _tick_full(conn)
        assert spawned == []
        assert res.placement_waits[tid].startswith("carry_pending:ace-ai: larger than a one-shot tick copies")
        assert not any(c[0] == "rsync" for c in loop.calls)
        assert not _events(conn, tid, kcb.STARTED_EVENT)
        _waits, spawned = _carry_until_claimed(conn, tid)    # the gateway tick (defer) carries it
    assert (tid, "ace-ai") in spawned


def test_one_shot_copy_steps_are_bounded_by_the_old_timeout(board, loop, monkeypatch):
    """Item 2: the inline copy runs each step under CARRY_TIMEOUT_SECONDS, as before this card."""
    seen = []
    real = loop.__call__

    def spy(argv, **kw):
        if argv[0] == "rsync":
            seen.append(kw.get("timeout"))
        return real(argv, **kw)
    monkeypatch.setattr(kcb, "_run", spy)
    with kb.connect_closing() as conn:
        tid, _path = _card(conn)
        _res, spawned = _tick_full(conn)
    assert (tid, "ace-ai") in spawned and seen == [kwh.CARRY_TIMEOUT_SECONDS]


def test_a_finished_copy_is_claimed_even_when_ticks_are_further_apart_than_fresh_s(board, loop, monkeypatch):
    """Item 4: ticks more than FRESH_S apart. The first tick that sees the finished copy claims it; it is not
    refreshed into another copy on every tick."""
    monkeypatch.setattr(kcb, "FRESH_S", 0.0)    # every finished copy counts as older than FRESH_S
    with kb.connect_closing() as conn:
        tid, _path = _card(conn)
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        time.sleep(0.01)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
    assert (tid, "ace-ai") in spawned
    assert sum(1 for c in loop.calls if c[0] == "rsync") == 1


def test_finalize_into_a_nonempty_workspace_copies_a_file_of_equal_size_and_mtime(board, loop, tmp_path):
    """Prism 707d9e8389dd: the merge finalize must not skip a staged file whose size and mtime equal the old one."""
    with kb.connect_closing() as conn:
        _tid, path = _card(conn)
    host = _plan(free=2).hosts["ace-ai"]
    dest = Path(loop._map(str(path)))
    dest.mkdir(parents=True)
    (dest / "notes.md").write_text("old\n")
    (dest / "keep").write_text("k\n")
    staging = Path(loop._map(kcb.staging_path(str(path))))
    staging.mkdir(parents=True)
    (staging / "notes.md").write_text("new\n")
    st = (dest / "notes.md").stat()
    os.utime(staging / "notes.md", ns=(st.st_atime_ns, st.st_mtime_ns))
    kcb.finalize(host, str(path), runner=loop)
    assert (dest / "notes.md").read_text() == "new\n" and not staging.exists()


# -- cases of the capped suites (pinned to carry_caps: true there) re-covered on the default background path --------

def test_default_path_a_carried_card_reruns_on_the_carry_host_and_is_never_carried_again(board, loop):
    """test_kanban_studio_pins_and_carry::test_a_carried_card_reruns_on_the_carry_host_never_on_the_stale_studio_copy,
    on the background path: the workspace_carried event binds every later run to the carry host."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _carry_until_claimed(conn, tid)
        assert kwh.carried_host(conn, tid, str(path)) == "ace-ai"
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        conn.commit()
        n = sum(1 for c in loop.calls if c[0] == "rsync")
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        assert len(_events(conn, tid, kcb.STARTED_EVENT)) == 1
    assert (tid, "ace-ai") in spawned
    assert sum(1 for c in loop.calls if c[0] == "rsync") == n   # no second copy over the remote work


def test_default_path_a_shared_workspace_is_never_carried(board, loop):
    """test_kanban_studio_pins_and_carry::test_a_shared_workspace_is_never_carried, on the background path."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        other = kb.create_task(conn, title="o", assignee="alpha")
        kbw.set_workspace_path(conn, other, str(path))
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        assert not _events(conn, tid, kcb.STARTED_EVENT)
    assert all(t != tid for t, _h in spawned) and loop.calls == []


def test_default_path_a_failed_finalize_is_a_spawn_failure_never_an_empty_dir_run(board, loop, monkeypatch):
    """test_kanban_studio_pins_and_carry::test_local_files_carry_failure_is_a_spawn_failure_never_an_empty_dir_run,
    on the background path: a finalize that fails releases the claim; no worker starts and no carried event."""
    def boom(*a, **k):
        raise RuntimeError("finalize rc=1")
    monkeypatch.setattr(kcb, "finalize", boom)
    with kb.connect_closing() as conn:
        tid, _path = _card(conn)
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        assert not _events(conn, tid, kwh.CARRIED_EVENT)
        status = kb.get_task(conn, tid).status
    assert all(t != tid for t, _h in spawned) and status != "running"


# -- the acceptance fixture: >= 3 GiB, >= 120,000 entries (opt-in) --------------------------------------------------

@pytest.mark.slow_test(3000, reason="writes and carries a 3 GiB / 120,000-entry fixture; opt-in only")
@pytest.mark.skipif(os.environ.get("CARRY_BIG_FIXTURE") != "1", reason="writes 3 GiB; CARRY_BIG_FIXTURE=1")
def test_three_gib_120k_entries_is_carried_and_the_tick_returns_fast(board, loop, capsys):
    import resource
    n_dirs, per_dir, size = 120, 1000, 27_000
    block = os.urandom(size)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="big", assignee="alpha")
        path = Path(kbw.resolve_workspace(kb.get_task(conn, tid)))
        for d in range(n_dirs):
            sub = path / f"d{d:03d}"
            sub.mkdir(parents=True, exist_ok=True)
            for f in range(per_dir):
                (sub / f"f{f:04d}").write_bytes(block)
        kbw.set_workspace_path(conn, tid, str(path))
        total, entries = kcb.census(str(path))
        assert total >= 3 * 1024 ** 3 and entries >= 120_000, (total, entries)
        assert not kwh.carry_size_ok(str(path))
        t0 = time.monotonic()
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        tick_s = time.monotonic() - t0
        assert res.placement_waits[tid] == "carry_pending:ace-ai" and spawned == []
        c0 = resource.getrusage(resource.RUSAGE_SELF)
        _wait_done(tid, timeout=3600)
        c1 = resource.getrusage(resource.RUSAGE_SELF)
        t1 = time.monotonic()
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        claim_tick_s = time.monotonic() - t1
        ev = _events(conn, tid, kwh.CARRIED_EVENT)[-1]
    dest = Path(loop._map(str(path)))
    file_bytes = lambda root: sum(_tree(root).values())  # noqa: E731 (dir st_size differs by creation order)
    with capsys.disabled():
        print(f"\nBIG CARRY: bytes={total} entries={entries} start_tick_s={tick_s:.3f} "
              f"claim_tick_s={claim_tick_s:.3f} carry={json.dumps(ev['background'])} "
              f"dispatcher_self_cpu_s={(c1.ru_utime - c0.ru_utime) + (c1.ru_stime - c0.ru_stime):.2f}")
    assert (tid, "ace-ai") in spawned
    assert kcb.census(str(dest))[1] == entries and file_bytes(dest) == file_bytes(path)
    assert tick_s < 5.0


# -- workspace admission runs before any copy (t_681a9c5e; Prism 2a7982d8eadd) --------------------------------------

def _protected_symlink_card(conn, board, tmp_path, monkeypatch):
    """A card whose recorded workspace root is protected (a ``workspace_mount_roots`` row, its parent the mount) and
    whose workspace path has been replaced by a symlink to a private directory outside that root."""
    tid, path = _card(conn)
    root = kb.workspaces_root()
    with kb.write_txn(conn):
        conn.execute("INSERT INTO workspace_mount_roots(root, mount_path) VALUES (?, ?)", (str(root), str(root.parent)))
    real_ismount = os.path.ismount
    monkeypatch.setattr("os.path.ismount", lambda p: Path(p) == root.parent or real_ismount(p))
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("private\n")
    import shutil
    shutil.rmtree(path)
    path.symlink_to(private, target_is_directory=True)
    return tid, path, private


@pytest.mark.parametrize("defer", [True, False], ids=["background", "one_shot"])
def test_a_workspace_admission_refuses_starts_no_carry(board, loop, tmp_path, monkeypatch, defer):
    """The background gate runs the claim's admission on the row's workspace first: a symlinked workspace root is
    refused before anything is counted or copied, and the refusal is the card's wait reason."""
    with kb.connect_closing() as conn:
        tid, path, private = _protected_symlink_card(conn, board, tmp_path, monkeypatch)
        # the per-row admission the ready loop runs earlier in the tick is bypassed here so the gate's own check is
        # what is under test (the path can be swapped for a link between the two)
        monkeypatch.setattr(kb, "_workspace_admission_refused", lambda *a, **k: False)
        res, spawned = _tick_full(conn, defer_pool_clone=defer)
        if kcb._jobs.get(tid):
            _wait_done(tid)
        started = _events(conn, tid, kcb.STARTED_EVENT)
    assert spawned == []
    assert started == []
    assert not any(c[0] == "rsync" for c in loop.calls), loop.calls
    assert "workspace symlink escape" in res.placement_waits[tid], res.placement_waits
    assert not Path(loop._map(kcb.staging_path(str(path)))).exists()
    assert (private / "secret.txt").read_text() == "private\n"


@pytest.mark.parametrize("where", ["carry_gate", "ready_loop"])
def test_a_staging_dir_left_by_an_earlier_carry_is_removed_when_admission_refuses(board, loop, tmp_path,
                                                                                 monkeypatch, where):
    """An earlier carry (a started event, its staging dir on the host) and then the workspace becomes a symlink out of
    the root: whichever admission refuses first in the tick (the ready loop's, or the carry gate's when the path was
    swapped in between) removes the staging dir under the carry lock and says so in one event."""
    with kb.connect_closing() as conn:
        tid, path, _private = _protected_symlink_card(conn, board, tmp_path, monkeypatch)
        kb._append_event(conn, tid, kcb.STARTED_EVENT, {"host": "ace-ai", "path": str(path), "background": True})
        conn.commit()
        if where == "carry_gate":
            monkeypatch.setattr(kb, "_workspace_admission_refused", lambda *a, **k: False)
        left = Path(loop._map(kcb.staging_path(str(path))))
        left.mkdir(parents=True)
        (left / "secret.txt").write_text("copied earlier\n")
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        refused = _events(conn, tid, kcb.REFUSED_EVENT)
        _tick_full(conn, defer_pool_clone=True)            # a second refused tick records nothing new
        assert len(_events(conn, tid, kcb.REFUSED_EVENT)) == 1
    why = res.placement_waits.get(tid) or dict(res.workspace_refused).get(tid)
    assert spawned == [] and "workspace symlink escape" in why, (res.placement_waits, res.workspace_refused)
    assert not left.exists()
    assert refused and refused[-1]["host"] == "ace-ai" and refused[-1]["staging_removed"] is True
    assert not any(c[0] == "rsync" for c in loop.calls)


def test_a_failed_staging_remove_records_nothing_and_is_retried(board, loop, tmp_path, monkeypatch):
    """Prism 8309def60720: a refusal whose staging remove failed writes no event, so the next refused tick removes it."""
    with kb.connect_closing() as conn:
        tid, path, _private = _protected_symlink_card(conn, board, tmp_path, monkeypatch)
        kb._append_event(conn, tid, kcb.STARTED_EVENT, {"host": "ace-ai", "path": str(path), "background": True})
        conn.commit()
        left = Path(loop._map(kcb.staging_path(str(path))))
        left.mkdir(parents=True)
        real_remove = kcb.remove_staging
        monkeypatch.setattr(kcb, "remove_staging", lambda *a, **k: False)
        _tick_full(conn, defer_pool_clone=True)
        assert _events(conn, tid, kcb.REFUSED_EVENT) == [] and left.exists()
        monkeypatch.setattr(kcb, "remove_staging", real_remove)
        _tick_full(conn, defer_pool_clone=True)
        refused = _events(conn, tid, kcb.REFUSED_EVENT)
    assert len(refused) == 1 and refused[0]["staging_removed"] is True
    assert not left.exists()


def test_a_staging_copy_is_kept_when_the_source_workspace_is_gone(board, loop, tmp_path, monkeypatch):
    """Prism 9ec61b3d7d14: admission refuses because the source is lost (stranded_by_mount_loss): the finished staging
    copy may be the only copy of the work, so it stays and no refusal is recorded."""
    from hermes_cli import kanban_db_dispatch as kdd
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        kb._append_event(conn, tid, kcb.STARTED_EVENT, {"host": "ace-ai", "path": str(path), "background": True})
        conn.commit()
        left = Path(loop._map(kcb.staging_path(str(path))))
        left.mkdir(parents=True)
        (left / "work.txt").write_text("the only copy\n")
        import shutil
        shutil.rmtree(path)                                 # the source is gone, not a policy violation
        row = {"id": tid, "workspace_path": str(path)}
        kdd._drop_refused_carry(conn, row, _plan(free=2), "default", f"stranded_by_mount_loss: {path}")
        refused = _events(conn, tid, kcb.REFUSED_EVENT)
    assert (left / "work.txt").read_text() == "the only copy\n"
    assert refused == []


def test_the_carry_gate_admits_the_path_it_copies(board, loop, tmp_path, monkeypatch):
    """Prism e310afb55ca1: the row's workspace (what the carry copies) is a symlink out of the root; the task read back
    names a safe path. The gate must not admit the safe path and copy the row's: it copies nothing this tick."""
    import dataclasses
    with kb.connect_closing() as conn:
        tid, path, _private = _protected_symlink_card(conn, board, tmp_path, monkeypatch)
        monkeypatch.setattr(kb, "_workspace_admission_refused", lambda *a, **k: False)
        safe = path.parent / (path.name + "-safe")
        safe.mkdir()
        real_get = kb.get_task
        monkeypatch.setattr(kb, "get_task", lambda c, t: (dataclasses.replace(real_get(c, t), workspace_path=str(safe))
                                                          if t == tid else real_get(c, t)))
        res, spawned = _tick_full(conn, defer_pool_clone=False)
        started = _events(conn, tid, kcb.STARTED_EVENT)
    assert spawned == [] and started == []
    assert not any(c[0] == "rsync" for c in loop.calls), loop.calls


def test_a_refresh_whose_submit_raises_is_not_handed_over_as_done(board, loop, tmp_path, monkeypatch):
    """Prism 64edba28c538: done_at / seen_done are cleared only once the refresh submit has returned."""
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(kcb, "FRESH_S", 0.0)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        host = _plan(free=2).hosts["ace-ai"]
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "started"
        _wait_done(tid)
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "done"

        class Broken:
            def submit(self, *a, **k):
                raise RuntimeError("pool shut down")
        real_executor = kcb._executor
        monkeypatch.setattr(kcb, "_executor", lambda: Broken())
        with pytest.raises(RuntimeError):
            kcb.poll(tid, host, str(path), lock_dir)   # the refresh could not be submitted
        monkeypatch.setattr(kcb, "_executor", real_executor)
        state, job = kcb.poll(tid, host, str(path), lock_dir)
        assert state == "running"                       # the refresh is retried, never skipped
        _wait_done(tid)
        monkeypatch.setattr(kcb, "FRESH_S", 3600.0)
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "done"
        kcb.take(tid, host, str(path))
        kcb.release(job)


# -- advisory read of PR 290 at d92b75eb, operator items 4 to 7 + the gitlink refusal (t_681a9c5e) -------------------

def test_finalize_replaces_a_symlinked_workspace_path_never_writes_through_it(board, loop, tmp_path):
    """Item 4: the workspace path on the host is a symlink to a dir holding one file. After finalize the path is a
    real directory with the staged files, and the old target is untouched."""
    with kb.connect_closing() as conn:
        _tid, path = _card(conn)
    host = _plan(free=2).hosts["ace-ai"]
    dest = Path(loop._map(str(path)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "link-target"
    target.mkdir()
    (target / "theirs.txt").write_text("theirs\n")
    dest.symlink_to(target, target_is_directory=True)
    staging = Path(loop._map(kcb.staging_path(str(path))))
    staging.mkdir(parents=True)
    (staging / "notes.md").write_text("staged\n")
    kcb.finalize(host, str(path), runner=loop)
    assert not dest.is_symlink() and dest.is_dir()
    assert (dest / "notes.md").read_text() == "staged\n" and not (dest / "theirs.txt").exists()
    assert sorted(p.name for p in target.iterdir()) == ["theirs.txt"]
    assert (target / "theirs.txt").read_text() == "theirs\n"
    assert not staging.exists()


def _linked_worktree(tmp_path, tmp_path_factory, path, branch):
    origin = _origin_with_many_files(tmp_path_factory.mktemp("origin"), 5)
    home = tmp_path / "home"
    _g("clone", "-q", str(origin), str(home))
    wt = path / "repo"
    _g("worktree", "add", "-q", "-b", branch, str(wt), cwd=home)
    return home, wt


def test_a_modified_file_with_the_size_and_mtime_of_head_arrives_modified(board, loop, tmp_path, tmp_path_factory,
                                                                          monkeypatch):
    """Item 5: same size, same mtime second, different content than the version the host checked out: the files step
    sends it. The host's checkout is given the Studio file's mtime just before the files rsync (the clash the quick
    check misses: a checkout and an edit in the same second, or a reused checkout)."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _home, wt = _linked_worktree(tmp_path, tmp_path_factory, path, "card/q")
        f = wt / "tracked.txt"
        assert f.read_text() == "v1\n"
        f.write_text("v2\n")                                        # same size as "v1\n"
        real = loop.__call__

        def same_mtime(argv, **kw):
            if argv[0] == "rsync" and "--files-from=-" in argv:
                host_copy = Path(loop._map(kcb.staging_path(str(path)))) / "repo" / "tracked.txt"
                assert host_copy.read_text() == "v1\n"
                st = f.stat()
                os.utime(host_copy, ns=(st.st_atime_ns, st.st_mtime_ns))
            return real(argv, **kw)
        monkeypatch.setattr(kcb, "_run", same_mtime)
        _carry_until_claimed(conn, tid)
    assert (Path(loop._map(str(wt))) / "tracked.txt").read_text() == "v2\n"


def test_a_local_origin_the_host_cannot_clone_falls_back_to_the_full_bundle(board, loop, tmp_path, tmp_path_factory,
                                                                            monkeypatch):
    """Item 6: a checkout whose origin is a Studio-local path that does not exist on the host: the clone on the host
    fails, the carry falls back to the full bundle once, and HEAD matches."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        home, wt = _linked_worktree(tmp_path, tmp_path_factory, path, "card/l")
        (wt / "new.txt").write_text("unpushed\n")
        _g("add", "new.txt", cwd=wt)
        _g("commit", "-qm", "unpushed", cwd=wt)
        url = _g("remote", "get-url", "origin", cwd=home)
        assert url.startswith("/")
        # the host side: that local path does not exist there (the loopback runs on this machine, so the remote
        # clone is pointed at a path that is absent; the Studio side keeps its real origin refs)
        real = kcb._origin_url
        monkeypatch.setattr(kcb, "_origin_url", lambda repo: str(tmp_path / "no-such-origin.git"))
        before = _git_view(wt)
        waits, _spawned = _carry_until_claimed(conn, tid)
        rebuilt = _events(conn, tid, kwh.CARRIED_EVENT)[-1]["background"]["rebuilt"]
        assert not _events(conn, tid, kcb.FAILED_EVENT), waits
        monkeypatch.setattr(kcb, "_origin_url", real)
    got = _git_view(Path(loop._map(str(wt))))
    assert rebuilt[0]["full_bundle"] is True
    assert (got[0], got[3]) == (before[0], before[3])


def test_the_mirror_warm_fetch_survives_a_branch_checked_out_by_another_workspace(tmp_path):
    """Operator 23:40 item A (t_e38aba52 23:26): the host's mirror has a branch checked out by another workspace's
    worktree. The rebuild's warm fetch must not be refused for it: origin's new HEAD reaches the mirror, the branch the
    other workspace has checked out is not moved, and the rebuild succeeds."""
    up = tmp_path / "up"
    _g("init", "-q", str(up))
    (up / "a").write_text("a\n")
    _g("add", "a", cwd=up)
    _g("commit", "-qm", "a", cwd=up)
    _g("branch", "feat", cwd=up)
    mirror = tmp_path / "m.git"
    _g("clone", "-q", "--mirror", str(up), str(mirror))
    # another workspace's linked worktree on the mirror with feat checked out (written by hand: the suite's guard
    # refuses `git worktree`); git's fetch reads exactly these files to refuse "checked out at"
    other = tmp_path / "other"
    other.mkdir()
    admin = mirror / "worktrees" / "other"
    admin.mkdir(parents=True)
    (admin / "HEAD").write_text("ref: refs/heads/feat\n")
    (admin / "commondir").write_text("../..\n")
    (admin / "gitdir").write_text(f"{other}/.git\n")
    (other / ".git").write_text(f"gitdir: {admin}\n")
    feat_before = _g("--git-dir", str(mirror), "rev-parse", "refs/heads/feat")
    for name in ("b", "c"):                               # origin moves main AND the checked-out branch
        if name == "c":
            _g("checkout", "-q", "feat", cwd=up)
        (up / name).write_text(name + "\n")
        _g("add", name, cwd=up)
        _g("commit", "-qm", name, cwd=up)
    _g("checkout", "-q", "main", cwd=up)
    head = _g("rev-parse", "HEAD", cwd=up)
    bundle = tmp_path / "b.bundle"
    _g("bundle", "create", str(bundle), "HEAD", cwd=up)
    plan = {"head": head, "url": str(up), "branch": "main", "stash": (), "exclude": None}
    proc = subprocess.run(["bash", "-c", kcb.rebuild_script(str(tmp_path / "dest"), plan, head, str(mirror))],
                          input=bundle.read_bytes(), capture_output=True)
    assert proc.returncode == 0, proc.stderr
    assert b"refusing to fetch" not in proc.stderr, proc.stderr
    assert subprocess.run(["git", "--git-dir", str(mirror), "cat-file", "-e", head + "^{commit}"]).returncode == 0
    assert _g("--git-dir", str(mirror), "rev-parse", "refs/heads/feat") == feat_before
    assert _g("rev-parse", "HEAD", cwd=tmp_path / "dest") == head


def test_a_full_bundle_records_what_the_host_lacked(board, loop, tmp_path, tmp_path_factory, monkeypatch):
    """Operator 23:40 item B: a rebuild that falls back to the full bundle says why (the prerequisite git named)."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _home, wt = _linked_worktree(tmp_path, tmp_path_factory, path, "card/r")
        monkeypatch.setattr(kcb, "_origin_url", lambda repo: str(tmp_path / "no-such-origin.git"))
        _carry_until_claimed(conn, tid)
        rebuilt = _events(conn, tid, kwh.CARRIED_EVENT)[-1]["background"]["rebuilt"]
    assert rebuilt[0]["full_bundle"] is True
    assert "lacks these prerequisite commits" in rebuilt[0]["full_reason"], rebuilt


def test_a_refreshed_rebuild_drops_an_ignored_file_deleted_on_the_studio(board, loop, tmp_path, tmp_path_factory,
                                                                         monkeypatch):
    """Item 7: the staging dir holds an ignored file from a first carry; the source no longer has it; after the second
    carry (a refresh into the same staging dir) it is gone."""
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(kcb, "FRESH_S", 0.0)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _home, wt = _linked_worktree(tmp_path, tmp_path_factory, path, "card/i")
        (wt / "secret.env").write_text("ignored\n")
        (wt / "kept.env").write_text("still here\n")
        host = _plan(free=2).hosts["ace-ai"]
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "started"
        _wait_done(tid)
        staged = Path(loop._map(kcb.staging_path(str(path)))) / "repo"
        assert (staged / "secret.env").exists()
        (wt / "secret.env").unlink()
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "done"
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "running"   # the refresh, into the same staging dir
        _wait_done(tid)
        monkeypatch.setattr(kcb, "FRESH_S", 3600.0)
        state, job = kcb.poll(tid, host, str(path), lock_dir)
        assert state == "done", job
        assert not (staged / "secret.env").exists()
        assert (staged / "kept.env").read_text() == "still here\n"
        kcb.take(tid, host, str(path))
        kcb.release(job)


def test_a_rebuilt_checkout_that_tracks_a_submodule_is_refused(board, loop, tmp_path, tmp_path_factory):
    """A rebuilt checkout with a gitlink is refused (the rebuild carries neither .git/modules nor the submodule's
    files), so the capped rule decides the card. Nothing is copied."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _home, wt = _linked_worktree(tmp_path, tmp_path_factory, path, "card/s")
        sha = _g("rev-parse", "HEAD", cwd=wt)
        _g("update-index", "--add", "--cacheinfo", f"160000,{sha},sub", cwd=wt)
        _g("commit", "-qm", "gitlink", cwd=wt)
        job = kcb.Job(tid, _plan(free=2).hosts["ace-ai"], str(path))
        with pytest.raises(kcb.CarryRefused, match="submodule"):
            kcb.run(job)
    assert not any(c[0] == "rsync" for c in loop.calls)
