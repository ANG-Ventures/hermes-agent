"""Terminating (or finding dead) a worker reaps its WHOLE session (t_ca0233d7).

Workers are spawned with ``start_new_session=True``, so worker pid == sid ==
pgid. Signalling only the worker pid left everything it had started in another
process group of that session alive with ppid 1 (54 headless Chromes from a
worker's pytest, Studio load1 250, 2026-09-29).

Each test runs a REAL fake worker: a session leader that starts a setpgrp'd
``/bin/sleep`` and then waits. Only processes the test created are ever
signalled.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

pytestmark = [
    pytest.mark.skipif(
        not hasattr(os, "getsid") or not hasattr(os, "killpg"), reason="POSIX sessions only",
    ),
    # A crashed worker's leftovers are reparented to init, i.e. outside the
    # test's process subtree by construction; they are still only the
    # processes this test started (session == the fake worker's sid).
    pytest.mark.live_system_guard_bypass,
]

_WORKER = (
    "import os, subprocess, sys, time\n"
    "p = subprocess.Popen(['/bin/sleep', '120'], preexec_fn=os.setpgrp)\n"
    "print(p.pid, flush=True)\n"
    "if sys.argv[1] == 'exit':\n"
    "    sys.stdin.readline()\n"  # crash only when the test says so
    "    os._exit(0)\n"
    "time.sleep(120)\n"
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    leftovers: list[int] = []
    monkeypatch.setattr(sys.modules[__name__], "_LEFTOVERS", leftovers)
    with kb.connect() as c:
        yield c
    for pid in leftovers:  # never leak the sleep, even when the test fails
        try:
            os.kill(pid, 9)
        except OSError:
            pass


_LEFTOVERS: list = []


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not kb._pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _worker_card(conn, mode: str, max_runtime=None):
    tid = kb.create_task(conn, title="card", assignee="worker",
                         max_runtime_seconds=max_runtime)
    assert kb.claim_task(conn, tid) is not None
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        start_new_session=True,
    )
    assert worker.stdout is not None
    sleep_pid = int(worker.stdout.readline())
    _LEFTOVERS.append(sleep_pid)
    threading.Thread(target=worker.wait, daemon=True).start()  # no zombie
    assert kbd._set_worker_pid(conn, tid, worker.pid)
    # The worker heartbeats (while alive) after starting its child; event
    # times are whole seconds, so step past the second the sleep was born in.
    time.sleep(1.1)
    with kb.write_txn(conn):
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
        ).fetchone()[0]
        kb._append_event(conn, tid, "heartbeat", {}, run_id=run_id)
    assert os.getsid(sleep_pid) == worker.pid
    assert os.getpgid(sleep_pid) == sleep_pid != worker.pid
    return tid, worker, sleep_pid


def _crash(worker) -> None:
    assert worker.stdin is not None
    worker.stdin.write("go\n")
    worker.stdin.flush()


def test_max_runtime_reaps_session_leftovers(conn):
    tid, worker, sleep_pid = _worker_card(conn, "stay", max_runtime=1)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (int(time.time()) - 30, tid),
        )
    assert tid in kbd.enforce_max_runtime(conn)
    assert _gone(worker.pid)
    assert _gone(sleep_pid), "setpgrp'd sleep outlived its worker's session"


def test_crashed_worker_session_leftovers_reaped(conn, monkeypatch):
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    tid, worker, sleep_pid = _worker_card(conn, "exit")
    assert kb._pid_alive(worker.pid)  # heartbeat above was sent while alive
    _crash(worker)
    assert _gone(worker.pid)
    assert kb._pid_alive(sleep_pid)
    kbd.detect_crashed_workers(conn)
    assert _gone(sleep_pid), "setpgrp'd sleep outlived its crashed worker"


def test_recycled_session_is_not_reaped(conn, monkeypatch):
    """Prism P1: a dead worker's pid reused by a NEW session leader that exited
    and left children. Those children postdate the recorded run's last
    evidence, so they are not the worker's and must survive."""
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    tid, worker, sleep_pid = _worker_card(conn, "exit")
    with kb.write_txn(conn):  # the recorded run ended 2 h ago
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 7200 "
            "WHERE task_id = ? AND kind IN ('claimed', 'spawned', 'heartbeat')", (tid,),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = started_at - 7200 "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)", (tid,),
        )
    _crash(worker)
    assert _gone(worker.pid)
    kbd.detect_crashed_workers(conn)
    time.sleep(0.5)
    assert kb._pid_alive(sleep_pid), "reaped a session that is not the recorded worker's"


_FORK_ON_TERM = (
    "import os, signal, subprocess, sys, time\n"
    "def h(*_):\n"
    "    p = subprocess.Popen(['/bin/sleep', '120'], preexec_fn=os.setpgrp)\n"
    "    print(p.pid, flush=True)\n"
    "    time.sleep(1)\n"  # still a member when the reaper rescans
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, h)\n"
    "os.setpgrp()\n"
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)


@pytest.mark.platforms("linux")
def test_group_born_during_a_slow_scan_is_reaped(monkeypatch):
    """t_db02e9c8: under load one session scan outlasts the forker's 1 s
    linger (ps over 15k procs at load1 387), so no two snapshots share a
    member. The group it created must still be reaped."""
    real = kb._worker_session_members

    def slow(sid):
        found = real(sid)
        time.sleep(1.5)  # snapshot taken, returned after the forker exited
        return found

    monkeypatch.setattr(kb, "_worker_session_members", slow)
    test_group_born_during_reap_is_reaped()


@pytest.mark.platforms("linux")
def test_group_born_during_reap_is_reaped():
    """Prism: a member that answers SIGTERM by forking into a NEW process
    group must not escape the reap (Linux clock; on the wall clock a group
    first seen after the reap began is left alone by design)."""
    leader = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "c = subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdout=sys.stdout)\n"
         "time.sleep(120)\n",
         _FORK_ON_TERM],
        stdout=subprocess.PIPE, text=True, encoding="utf-8", start_new_session=True,
    )
    assert leader.stdout is not None
    assert leader.stdout.readline().strip() == "ready"
    born_after = time.time() - 5
    leader.kill()
    leader.wait()
    reaped = kb._reap_worker_session(leader.pid, born_after=born_after,
                                     born_before=time.time())
    escaped = int(leader.stdout.readline())
    try:
        assert reaped >= 2  # the forker's group and the one it created
        assert _gone(escaped), "group created on SIGTERM escaped the reap"
    finally:
        try:
            os.kill(escaped, 9)
        except OSError:
            pass


def test_reap_stops_when_session_continuity_breaks(monkeypatch):
    """Prism: ownership must hold for every rescan, not just the first. A
    rescan whose members were all born after every earlier member died is a
    reused sid; nothing in it may be signalled."""
    births = {111: 100.0}
    sids = {111: 99_999}
    calls: list = []

    def scan(sid):
        calls.append(sid)
        if len(calls) == 1:
            return [(111, 111)]
        births.pop(111, None)       # the original session emptied ...
        sids.pop(111, None)
        births[222] = time.time()   # ... and a later process reused its sid
        sids[222] = 99_999
        return [(222, 222)] if len(calls) == 2 else []

    signalled = _fake_world(monkeypatch, scan, births, sids)
    assert kb._reap_worker_session(99_999, born_after=50.0, born_before=200.0,
                                   grace=0.5) == 1
    assert signalled == [111], "signalled a group from a non-continuous rescan"


def _fake_world(monkeypatch, scan, births, sids, pgids=None) -> list:
    """Wire the reaper to a fake process table; returns the signalled pgids."""
    signalled: list = []
    real_getsid = os.getsid
    pgids = {} if pgids is None else pgids

    def record(pid, sid):  # one observation: sid, pgid and birth together
        if sids.get(pid) != sid:
            return None
        born = kb._reap_birth(pid)
        return None if born is None else (pgids.get(pid, pid), born)

    def getsid(pid):
        if pid == 0:
            return real_getsid(0)
        if pid not in sids:
            raise ProcessLookupError(pid)
        return sids[pid]

    monkeypatch.setattr(kb, "_worker_session_members", scan)
    monkeypatch.setattr(kb, "_member_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(kb, "_reap_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(kb, "_reap_record", record, raising=False)
    monkeypatch.setattr(kb, "_reap_now", time.time)
    monkeypatch.setattr(kb.os, "getsid", getsid)
    monkeypatch.setattr(kb.os, "killpg", lambda pg, sig: signalled.append(pg))
    return signalled


def test_birth_read_early_never_vouches_for_a_reused_sid(monkeypatch):
    """Prism fe32a904b73f: a birth clock that reads early (Linux epoch
    create_time floors btime to a whole second) must not let a replacement
    born AFTER the last proof pass as born before it. Last proof 1000.006;
    the sid is reused at 1000.009 and that birth reads 1000.000."""
    births = {111: 900.0}
    sids = {111: 99_999}
    calls: list = []

    def scan(sid):
        calls.append(sid)
        if len(calls) == 1:
            return [(111, 111)]
        births.pop(111, None)       # the original session emptied ...
        sids.pop(111, None)
        births[222] = 1000.000      # ... reused at 1000.009, read 9 ms early
        sids[222] = 99_999
        return [(222, 222)]

    signalled = _fake_world(monkeypatch, scan, births, sids)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    monkeypatch.setattr(kb, "_reap_now", lambda: 1000.006)  # every proof
    kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=0.5)
    assert signalled == [111], "a birth read early vouched for a reused sid"


def test_group_born_during_reap_is_signalled_once_a_later_proof_covers_it(monkeypatch):
    """The slack only delays: a group born during the reap, first seen after
    the member that forked it is gone, is signalled once that member was
    proved alive more than the slack after the group's birth."""
    births = {111: 900.0}
    sids = {111: 99_999}
    clock = [1000.0]
    calls: list = []

    def now():
        clock[0] += 0.05
        return clock[0]

    def scan(sid):
        calls.append(sid)
        if len(calls) == 1:         # 111 forks 333 into a new group on SIGTERM
            births[333] = clock[0]
            sids[333] = 99_999
        if len(calls) < 5:          # four proofs: 0.15 s past 333's birth
            return [(111, 111)]
        births.pop(111, None)
        sids.pop(111, None)
        return [(333, 333)]         # first seen alone, after its parent exited

    signalled = _fake_world(monkeypatch, scan, births, sids)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    monkeypatch.setattr(kb, "_reap_now", now)
    kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=1.0)
    assert set(signalled) == {111, 333}, "a group born during the reap escaped"


def test_known_member_born_inside_the_slack_is_sigkilled(monkeypatch):
    """Prism f5b8e6a3daaf: with grace=0, a proven member that ignores SIGTERM
    and was born 0.05 s before the last proof is a member by identity, so the
    birth slack must not spare it from SIGKILL."""
    import signal

    births = {111: 1000.00}
    sids = {111: 99_999}
    _fake_world(monkeypatch, lambda sid: [(111, 111)], births, sids)
    sent: list = []
    monkeypatch.setattr(kb.os, "killpg", lambda pg, sig: sent.append((pg, sig)))
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    monkeypatch.setattr(kb, "_reap_now", lambda: 1000.05)  # every proof
    kb._reap_worker_session(99_999, born_after=999.0, born_before=1000.02, grace=0)
    assert (111, signal.SIGKILL) in sent, f"known member left running: {sent}"


def test_wall_clock_step_mid_reap_signals_no_member_first_seen_after_it(monkeypatch):
    """Prism a7748c722865: on the fallback (wall) clock, a 5 s forward step
    mid-reap moves the proof past births that follow the true proof. A
    replacement session born after the original emptied must not be
    signalled on the strength of that stepped proof."""
    real_time = time.time
    step = [0.0]
    births = {111: real_time() - 100}
    sids = {111: 99_999}
    calls: list = []

    def scan(sid):
        calls.append(sid)
        if len(calls) == 1:             # ownership check, before the reap began
            return [(111, 111)]
        if len(calls) == 2:
            step[0] = 5.0               # mid-reap the wall clock steps forward ...
            return [(111, 111)]         # ... while 111 lives: proofs read +5 s
        births.pop(111, None)           # the original session empties ...
        sids.pop(111, None)
        births[222] = real_time()       # ... and its sid is reused, unstepped birth
        sids[222] = 99_999
        return [(222, 222)]

    signalled = _fake_world(monkeypatch, scan, births, sids)
    fake_time = type(sys)("fake_time")
    fake_time.__dict__.update(vars(time))
    fake_time.time = lambda: real_time() + step[0]
    monkeypatch.setattr(kb, "time", fake_time)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", False)
    monkeypatch.setattr(kb, "_reap_now", lambda: kb.time.time())
    kb._reap_worker_session(99_999, born_after=births[111] - 1,
                            born_before=births[111] + 1, grace=1.0)
    assert 222 not in signalled, f"signalled a member first seen after the step: {signalled}"
    assert signalled == [111]


def test_wall_clock_excursion_during_known_member_proofs_is_latched(monkeypatch, caplog):
    """Prism be979415d495: the wall clock steps +5 s while proofs re-see a
    known member, then steps back before the first disjoint snapshot. The
    excursion future-dated ``proved_at``, so it must be latched at the proof
    even though the offset reads normal again by the time the replacement is
    seen."""
    real_time = time.time
    step = [0.0]
    births = {111: real_time() - 100}
    sids = {111: 99_999}
    calls: list = []

    def scan(sid):
        calls.append(sid)
        if len(calls) == 1:             # ownership check, before the reap began
            return [(111, 111)]
        if len(calls) == 2:
            step[0] = 5.0               # forward excursion while 111 lives ...
            time.sleep(0.12)            # ... long enough for in-scan proofs
            return [(111, 111)]
        step[0] = 0.0                   # ... and back before the next snapshot
        births.pop(111, None)
        sids.pop(111, None)
        births[222] = real_time()       # sid reused after the true proof
        sids[222] = 99_999
        return [(222, 222)]

    signalled = _fake_world(monkeypatch, scan, births, sids)
    fake_time = type(sys)("fake_time")
    fake_time.__dict__.update(vars(time))
    fake_time.time = lambda: real_time() + step[0]
    monkeypatch.setattr(kb, "time", fake_time)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", False)
    monkeypatch.setattr(kb, "_reap_now", lambda: kb.time.time())
    with caplog.at_level("INFO", logger=kb._log.name):
        kb._reap_worker_session(99_999, born_after=births[111] - 1,
                                born_before=births[111] + 1, grace=1.0)
    assert 222 not in signalled, f"a reverted excursion vouched for a reused sid: {signalled}"
    assert signalled == [111]
    assert "first seen after the reap began" in caplog.text


def test_pid_reused_outside_the_session_is_not_a_member(monkeypatch):
    """Prism 8c1178a4f868: A (111) forks group leader B (222) during a scan
    that captures both; B exits and pid 222 becomes an unrelated group leader
    in another session; A is proved alive past the slack, then exits before
    the snapshot is read. B's stale pgid must not be signalled."""
    births = {111: 900.0}
    sids = {111: 99_999}
    clock = [1000.0]
    calls: list = []

    def now():
        clock[0] += 0.05
        return clock[0]

    def scan(sid):
        calls.append(sid)
        if len(calls) == 2:
            births[222] = clock[0]          # B, a member, captured below ...
            sids[222] = 99_999
            snap = [(111, 111), (222, 222)]
            births[222] = clock[0] + 0.01   # ... exits; 222 reused outside S
            sids[222] = 55_555
            time.sleep(0.4)                 # A proved alive meanwhile
            births.pop(111, None)           # A exits before the snapshot is read
            sids.pop(111, None)
            return snap
        return [(111, 111)] if len(calls) == 1 else []

    signalled = _fake_world(monkeypatch, scan, births, sids)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    monkeypatch.setattr(kb, "_REAP_BIRTH_SLACK_SECONDS", 0.1)
    monkeypatch.setattr(kb, "_reap_now", now)
    kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=1.0)
    assert 222 not in signalled, f"signalled a reused pid's group: {signalled}"
    assert signalled == [111]


def _overlap_world(monkeypatch, *, original_empties: bool) -> list:
    """Known member 111; the second snapshot also holds new pid 222, born
    after every proof so only a continuity proof can admit it. When 222's
    record is read, 111 has exited (``original_empties``) or still lives."""
    births = {111: 900.0}
    sids = {111: 99_999}
    calls: list = []

    def scan(sid):
        calls.append(sid)
        return [(111, 111)] if len(calls) != 2 else [(111, 111), (222, 222)]

    signalled = _fake_world(monkeypatch, scan, births, sids)
    plain = kb._reap_record

    def record(pid, sid):
        if pid == 222 and 222 not in sids:
            if original_empties:          # the last known member exits ...
                births.pop(111, None)
                sids.pop(111, None)
            births[222] = 1000.5          # ... before 222's record is read
            sids[222] = 99_999
        return plain(pid, sid)

    monkeypatch.setattr(kb, "_reap_record", record)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    monkeypatch.setattr(kb, "_reap_now", lambda: 1000.0)
    kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=0.5)
    return signalled


def test_overlap_without_a_later_proof_admits_no_new_identity(monkeypatch):
    """Prism 023d7bc5df9b: an overlapping known identity does not vouch for
    a new identity whose record was read after the original session emptied."""
    signalled = _overlap_world(monkeypatch, original_empties=True)
    assert 222 not in signalled, f"overlap admitted a replacement's group: {signalled}"
    assert signalled == [111]


def test_overlap_with_a_later_proof_admits_the_new_identity(monkeypatch):
    """The same snapshot while 111 still lives: re-reading 111 after 222's
    record proves the session was continuous, so 222's group is reaped."""
    signalled = _overlap_world(monkeypatch, original_empties=False)
    assert set(signalled) == {111, 222}, f"a covered member escaped: {signalled}"


def test_pid_replaced_during_the_ownership_read_signals_nothing(monkeypatch):
    """Prism 3dfe1baa6ab0: the owned birth and the record that enters
    ``known`` must be one observation. 111 passes the run window, then its pid
    is reused by a process in the same sid (group 777) before the record is
    read again: the bracket fails, nothing is signalled."""
    births = {111: 900.0}
    sids = {111: 99_999}
    signalled = _fake_world(monkeypatch, lambda sid: [(111, 111)], births, sids,
                            pgids={})

    def birth(pid):
        born = births.get(pid)
        if pid == 111 and born == 900.0:   # the ownership read sees the original ...
            births[111] = 1000.5           # ... then the pid is reused
            kb_pgids[111] = 777
        return born

    kb_pgids: dict = {}
    plain = kb._reap_record
    monkeypatch.setattr(kb, "_member_birth", birth)
    monkeypatch.setattr(kb, "_reap_record", lambda pid, sid: (
        None if (rec := plain(pid, sid)) is None else (kb_pgids.get(pid, rec[0]), rec[1])))
    assert kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0,
                                   grace=0) == 0
    assert signalled == [], f"signalled a replacement read after ownership: {signalled}"


def test_wall_clock_never_adopts_a_member_first_seen_later(monkeypatch, caplog):
    """Fallback clock (operator ruling on Prism a7748c722865/be979415d495):
    no birth is ordered against a proof, so a reused sid's process reading
    as born long before every proof (a stepped wall clock) is never
    signalled."""
    births = {111: 900.0}
    sids = {111: 99_999}
    calls: list = []

    def scan(sid):
        calls.append(sid)
        if len(calls) == 1:
            return [(111, 111)]
        births.pop(111, None)
        sids.pop(111, None)
        births[222] = 500.0          # reads as born before any proof
        sids[222] = 99_999
        return [(222, 222)]

    signalled = _fake_world(monkeypatch, scan, births, sids)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", False)
    with caplog.at_level("INFO", logger=kb._log.name):
        kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=0.5)
    assert signalled == [111], f"wall clock adopted a later process: {signalled}"
    assert "first seen after the reap began" in caplog.text


def test_member_replaced_before_its_signal_is_not_signalled(monkeypatch):
    """The last-moment revalidation: an identity read and proved, then
    replaced by another session's process before its group is signalled."""
    births = {111: 900.0}
    sids = {111: 99_999}
    reads: list = []
    signalled = _fake_world(monkeypatch, lambda sid: [(111, 111)], births, sids)

    def record(pid, sid):
        reads.append(pid)
        if len(reads) > 3:            # after the bracketed identity read and the proof
            return None               # 111 is now another session's process
        return (pid, births[pid]) if sids.get(pid) == sid else None

    monkeypatch.setattr(kb, "_reap_record", record)
    kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=0)
    assert signalled == [], f"signalled a replaced member: {signalled}"


_BSD = (4242, 4242, 1_000_000, 123_456)  # pid, pgid, start sec, start usec


def _bracket(monkeypatch, reads, sid=7, pgid=4242):
    it = iter(reads)

    def getsid(pid):
        if isinstance(sid, BaseException):
            raise sid
        return sid

    monkeypatch.setattr(kb.os, "getsid", getsid)
    monkeypatch.setattr(kb.os, "getpgid", lambda pid: pgid)
    return kb._bracketed_member(4242, 7, read=lambda pid: next(it))


def test_bracketed_member_accepts_a_stable_member(monkeypatch):
    assert _bracket(monkeypatch, [_BSD, _BSD]) == (4242, 1_000_000.123456)


@pytest.mark.parametrize("reads, kw", [
    pytest.param([_BSD, (4242, 4242, 1_000_001, 0)], {}, id="pid-replaced-between-reads"),
    pytest.param([_BSD, _BSD], {"pgid": 4243}, id="pgid-mismatch"),
    pytest.param([(4242, 4243, 1_000_000, 123_456)] * 2, {}, id="record-pgid-mismatch"),
    pytest.param([(4241, 4242, 1_000_000, 123_456)] * 2, {}, id="record-names-another-pid"),
    pytest.param([_BSD, _BSD], {"sid": 8}, id="sid-mismatch"),
    pytest.param([_BSD, None], {}, id="disappeared"),
    pytest.param([None, None], {}, id="no-record"),
    pytest.param([_BSD, _BSD], {"sid": PermissionError(1, "denied")}, id="permission"),
])
def test_bracketed_member_is_unknown_on_any_inconclusive_read(monkeypatch, reads, kw):
    assert _bracket(monkeypatch, reads, **kw) is None


def test_inconclusive_bracket_signals_nothing_and_proves_nothing(monkeypatch):
    """A member whose pid is replaced between the two native reads is UNKNOWN:
    the reap neither signals it nor counts it as proof the session lives."""
    births = {111: 900.0}
    sids = {111: 99_999}
    signalled = _fake_world(monkeypatch, lambda sid: [(111, 111)], births, sids)
    flip = [0]

    def read(pid):
        flip[0] += 1
        return (pid, pid, 900 + flip[0] % 2, 0)  # births disagree on every pair

    monkeypatch.setattr(kb.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(kb, "_reap_record",
                        lambda pid, sid: kb._bracketed_member(pid, sid, read=read))
    assert kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0,
                                   grace=0.5) == 0
    assert signalled == []


@pytest.mark.platforms("linux", "macos")
def test_reap_record_reads_a_real_member_and_refuses_another_session():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         start_new_session=True)
    try:
        rec = kb._reap_record(p.pid, p.pid)
        assert rec is not None and rec[0] == p.pid
        assert kb._reap_record(p.pid, os.getsid(0)) is None
    finally:
        p.kill()
        p.wait(timeout=5)
    assert kb._reap_record(p.pid, p.pid) is None


@pytest.mark.platforms("linux", "macos")
def test_reap_birth_never_reads_earlier_than_the_slack():
    """The contract the continuity proof rests on: a process's birth, read on
    the reap clock, is never earlier than the moment before it was spawned by
    more than the slack (Linux epoch create_time reads up to ~1 s early)."""
    before = kb._reap_now()
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        after = kb._reap_now()
        born = kb._reap_birth(p.pid)
        assert born is not None
        assert before - kb._REAP_BIRTH_SLACK_SECONDS <= born <= after
    finally:
        p.kill()
        p.wait(timeout=5)


def test_reap_refuses_a_session_that_empties_before_the_proof(monkeypatch):
    """Prism 049b2dc2550d: ownership checked, then the last member exits and
    the sid is reused before the reap starts. Nothing may be signalled."""
    births = {111: 100.0}
    sids = {111: 99_999}
    lookups: list = []

    def birth(pid):
        lookups.append(pid)
        if len(lookups) > 1:        # gone right after the ownership check
            births.pop(111, None)
            sids.pop(111, None)
            births[222] = 150.0     # reused sid, born before any later cutoff
            sids[222] = 99_999
        return births.get(pid)

    signalled = _fake_world(monkeypatch, lambda sid: [(111, 111)] if not lookups
                            else [(222, 222)], births, sids)
    monkeypatch.setattr(kb, "_member_birth", birth)
    monkeypatch.setattr(kb, "_reap_birth", birth)
    assert kb._reap_worker_session(99_999, born_after=50.0, born_before=200.0,
                                   grace=0.5) == 0
    assert signalled == []


def test_member_that_left_the_session_proves_nothing(monkeypatch):
    """Prism 0c8eea80150d: a member that setsid()s away and stays alive must not
    keep vouching for its old sid once another session reuses it."""
    births = {111: 100.0}
    sids = {111: 99_999}
    calls: list = []

    def scan(sid):
        calls.append(sid)
        if len(calls) == 1:
            return [(111, 111)]
        sids[111] = 55_555          # still alive, now in its own session
        births[222] = time.time()   # the old sid reused by an unrelated process
        sids[222] = 99_999
        return [(222, 222)]

    signalled = _fake_world(monkeypatch, scan, births, sids)
    kb._reap_worker_session(99_999, born_after=50.0, born_before=200.0, grace=0.5)
    assert 222 not in signalled, "a detached member vouched for a reused sid"


def test_reap_refuses_init_and_own_session():
    window = {"born_after": 0.0, "born_before": time.time()}
    assert kb._reap_worker_session(0, **window) == 0
    assert kb._reap_worker_session(1, **window) == 0
    assert kb._reap_worker_session(os.getsid(0), **window) == 0
    assert kb._reap_worker_session(os.getpid(), **window) == 0


# --- run-identity sweep: children that setsid() OUT of the worker session -------------

def _spawn_escapee(task_id: str, run_id: str) -> subprocess.Popen:
    """A child in its OWN session (as browser_harness.daemon does) carrying the run identity."""
    env = {**os.environ, "HERMES_KANBAN_TASK": task_id, "HERMES_KANBAN_RUN_ID": run_id}
    p = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(120)"],
                         stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env, start_new_session=True)
    assert p.stdout is not None and p.stdout.readline().strip() == "ready"
    return p


def test_env_identity_reap_kills_a_daemon_that_left_the_session():
    """The browser-use harness daemon setsid()s, so the session reap never sees it;
    its environment still names the run. Born inside the window => reaped."""
    born_after = time.time() - 5
    victim = _spawn_escapee("t_envreap1", "4242")
    bystander = _spawn_escapee("t_envreap1", "4243")   # same task, a NEWER run: not ours
    try:
        # the session reap of the WORKER (a fake sid here: the victim is in its own session) sees nothing
        assert kb._reap_worker_session(99_999_9, born_after=born_after, born_before=time.time()) == 0
        assert not _gone(victim.pid)
        n = kb._reap_run_env_escapees("t_envreap1", 4242, born_after=born_after,
                                      born_before=time.time(), grace=2.0)
        assert n == 1
        assert _gone(victim.pid), "run-identified escapee survived the reap"
        assert not _gone(bystander.pid), "a different run's process was signalled"
    finally:
        for p in (victim, bystander):
            try:
                p.kill(); p.wait(timeout=5)
            except Exception:
                pass


def test_env_identity_reap_respects_birth_window_and_unknown_bounds():
    victim = _spawn_escapee("t_envreap2", "7")
    try:
        # born AFTER the window closed -> a recycled/unrelated process; untouched
        assert kb._reap_run_env_escapees("t_envreap2", 7, born_after=1.0, born_before=2.0) == 0
        assert not _gone(victim.pid)
        # unknown bounds -> no reap, ever
        assert kb._reap_run_env_escapees("t_envreap2", 7, born_after=None, born_before=time.time()) == 0
        assert kb._reap_run_env_escapees("t_envreap2", 7, born_after=0.0, born_before=None) == 0
        assert kb._reap_run_env_escapees(None, 7, born_after=0.0, born_before=time.time()) == 0
        assert not _gone(victim.pid)
    finally:
        victim.kill(); victim.wait(timeout=5)


def test_env_identity_scan_never_lists_self_or_own_session(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_envreap3")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "99")
    # this very process carries the identity and must be invisible to the sweep
    assert all(pid != os.getpid() for pid, _ in kb._run_env_escapees("t_envreap3", 99))
    assert kb._reap_run_env_escapees("t_envreap3", 99, born_after=0.0, born_before=time.time() + 10) == 0


def test_terminated_run_reap_never_targets_the_cards_newer_run(conn):
    """Prism a8211e2aced2: the post-termination sweep must reap the run that was
    terminated, not ``tasks.current_run_id``. When a claim is lost, another
    dispatcher may already have started a NEWER run on the card; its freshly
    spawned daemons fall inside the old run's birth window, and reading the
    DB pointer would SIGTERM/SIGKILL them."""
    tid = kb.create_task(conn, title="card", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    old_run = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
    ).fetchone()[0]
    new_run = int(old_run) + 1000
    with kb.write_txn(conn):   # the card has moved on to a newer run
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (new_run, tid))
    born_after = time.time() - 5
    victim = _spawn_escapee(tid, str(old_run))
    bystander = _spawn_escapee(tid, str(new_run))
    try:
        info: dict = {}
        kb._reap_terminated_worker_session(
            99_999_9, info, None, (born_after, None, None), time.time(), conn, tid,
            old_run,
        )
        assert info.get("env_escapees_reaped") == 1
        assert _gone(victim.pid), "the terminated run's escapee survived"
        assert not _gone(bystander.pid, timeout=1.0), "the newer run's process was signalled"
        # No run identity -> no run-scoped sweep; never fall back to the DB pointer.
        info = {}
        kb._reap_terminated_worker_session(
            99_999_9, info, None, (born_after, None, None), time.time(), conn, tid,
        )
        assert "env_escapees_reaped" not in info
        assert not _gone(bystander.pid, timeout=1.0)
    finally:
        for p in (victim, bystander):
            try:
                p.kill(); p.wait(timeout=5)
            except Exception:
                pass


@pytest.mark.parametrize("linux_clock", [True, False], ids=["linux-clock", "wall-clock"])
def test_initial_member_born_after_the_last_heartbeat_is_reaped_with_its_session(monkeypatch, linux_clock):
    """Prism 72790abed13c / 0d19e75007b0: a child the worker started AFTER its last
    heartbeat (born after born_before) but already in the INITIAL snapshot, in its own
    process group, is this session's: an owned member read alive after the snapshot
    proves the sid was never reused in between. It must get SIGTERM with its session,
    on either clock. Red at 293a10412d: only group 111 is signalled."""
    births = {111: 900.0, 222: 960.0}       # 222 born after born_before=950
    sids = {111: 99_999, 222: 99_999}
    pgids = {111: 111, 222: 222}

    def scan(sid):
        return [(pid, pgids[pid]) for pid in sorted(sids) if sids[pid] == sid]

    signalled = _fake_world(monkeypatch, scan, births, sids, pgids)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", linux_clock)
    kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=0.3)
    assert 222 in signalled, f"initial child born after the heartbeat left running: {signalled}"
    assert 111 in signalled


def test_initial_snapshot_member_is_not_adopted_when_no_owned_member_proves_continuity(monkeypatch):
    """The widened initial set rides ONLY on the post-snapshot proof: if the owned
    member is gone by then (the session may have emptied and the sid been reused),
    nothing is reaped, the later-born member included."""
    births = {111: 900.0, 222: 960.0}
    sids = {111: 99_999, 222: 99_999}
    pgids = {111: 111, 222: 222}
    reads = {"n": 0}
    real = {}

    def scan(sid):
        return [(pid, pgids[pid]) for pid in sorted(sids) if sids[pid] == sid]

    signalled = _fake_world(monkeypatch, scan, births, sids, pgids)
    real["record"] = kb._reap_record

    def record(pid, sid):
        reads["n"] += 1
        if pid == 111 and reads["n"] > 4:   # 111 dies after ownership + snapshot reads
            return None
        return real["record"](pid, sid)

    monkeypatch.setattr(kb, "_reap_record", record)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    assert kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=0.3) == 0
    assert signalled == []


def test_descendant_forked_before_the_post_record_proof_is_reaped(monkeypatch):
    """Prism 7e958a6f57d4: the post-record continuity proof's moment must advance
    proved_at. Owned 111 and a new member 222 are in a later snapshot; 222 forks
    group 333 at 1000.20, after the snapshot; the post-record proof of 111 succeeds
    at 1000.50; then 111 and 222 exit. 333 was born before a proof that showed the
    session continuous, so it is this session's and must be signalled. Red at
    3e9c908ed0 (333 judged against the stale 1000.05)."""
    births = {111: 900.0}
    sids = {111: 99_999}
    pgids = {111: 111}
    calls: list = []
    clock = {"t": 1000.05}

    def scan(sid):
        calls.append(sid)
        if len(calls) == 1:
            return [(111, 111)]
        if len(calls) == 2:
            births[222], sids[222], pgids[222] = 1000.10, 99_999, 222
            births[333], sids[333], pgids[333] = 1000.20, 99_999, 333
            return [(111, 111), (222, 222)]
        # 111 and 222 exited; only 333 is left in the session
        for p in (111, 222):
            births.pop(p, None)
            sids.pop(p, None)
        return [(333, 333)]

    def now():
        # in-scan proofs read 1000.05; the post-record proof of scan 2 reads 1000.50
        return clock["t"]

    signalled = _fake_world(monkeypatch, scan, births, sids, pgids)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    real_record = kb._reap_record

    def record(pid, sid):
        # _identities() reads 222 only AFTER scan 2 returned (every in-scan proof
        # already ran at 1000.05): the next proof, the post-record one, reads 1000.50
        if pid == 222 and len(calls) == 2:
            clock["t"] = 1000.50
        return real_record(pid, sid)

    monkeypatch.setattr(kb, "_reap_record", record)
    monkeypatch.setattr(kb, "_reap_now", now)
    kb._reap_worker_session(99_999, born_after=850.0, born_before=950.0, grace=0.5)
    assert 333 in signalled, f"descendant born before the post-record proof left running: {signalled}"


def test_early_read_epoch_birth_never_passes_ownership(monkeypatch):
    """Prism 44b9db65242a: Linux psutil epoch births read up to ~1 s early (integer
    btime). Last evidence 1010.0; the sid is reused and its group 222 is truly born
    at 1010.1, but psutil reads 1009.3. Ownership must subtract the epoch error, so
    222 is no witness and nothing is reaped. Red at d1dcb20b9d (222 signalled)."""
    births = {222: 1009.3}
    sids = {222: 99_999}
    pgids = {222: 222}

    def scan(sid):
        return [(pid, pgids[pid]) for pid in sorted(sids) if sids[pid] == sid]

    signalled = _fake_world(monkeypatch, scan, births, sids, pgids)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    monkeypatch.setattr(kb, "_EPOCH_BIRTH_EARLY_S", 1.02, raising=False)
    assert kb._reap_worker_session(99_999, born_after=1005.0, born_before=1010.0, grace=0.2) == 0
    assert signalled == [], f"an early-read replacement passed ownership: {signalled}"


def test_member_born_well_inside_the_run_still_owns_under_the_epoch_margin(monkeypatch):
    """The margin only narrows the top of the window: a member born more than the
    epoch error before the last evidence is still a witness."""
    births = {111: 1007.0}
    sids = {111: 99_999}
    pgids = {111: 111}

    def scan(sid):
        return [(pid, pgids[pid]) for pid in sorted(sids) if sids[pid] == sid]

    signalled = _fake_world(monkeypatch, scan, births, sids, pgids)
    monkeypatch.setattr(kb, "_REAP_LINUX_CLOCK", True)
    monkeypatch.setattr(kb, "_EPOCH_BIRTH_EARLY_S", 1.02, raising=False)
    kb._reap_worker_session(99_999, born_after=1005.0, born_before=1010.0, grace=0.2)
    assert 111 in signalled
