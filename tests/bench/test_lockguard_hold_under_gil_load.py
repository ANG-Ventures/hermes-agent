"""``hermes_state_lockguard.hold()`` stays O(1) in the fd-table size under GIL load (t_aeae3fe7).

2026-10-24 14:24-15:10 PT, live Mac Studio gateway: Discord replies landed 1-16 min after the agent
finished them (``response ready`` -> ``Sending response`` p50 64-119 s, max 963 s). py-spy showed 99%
of the samples HOLDING ``SessionDB._lock`` inside ``_own_fds_for`` (``_try_wal_checkpoint`` ->
``lockguard.hold``): ``os.listdir('/dev/fd')`` + one ``os.fstat`` per descriptor, each fstat
dropping the GIL and queueing behind every busy thread. Standalone repro with a 497-fd table:
quiet 0.3 ms; 4 GIL-busy threads 2.9-3.1 s; 12 GIL-busy threads 16.4-27.7 s per ``hold()``.
This bench on the fd-scan guard (ace-media, 500 fds, 12 threads): 14.1 s, 16.4 s, 11.7 s.

Budget. Every GIL-releasing syscall pays one GIL handoff under load, independent of what it does:
a lone ``os.stat`` measured median 25-50 ms with 12 json-churn threads on ace-media. A private-fd
hold() is a handful of syscalls (2 stat, at most 2 open+fstat, 2 fcntl), so no CPython guard can
meet a flat 50 ms there. The bench therefore measures that floor in the same run and allows
``50 ms + 20 x floor``: the fd-table scan is ~1000 syscalls (RED by an order of magnitude), the
private guard is ~8. It also asserts the structural property directly: no fd-table listing.
"""

import json
import os
import sqlite3
import statistics
import threading
import time

import pytest

import hermes_state_lockguard as lg

pytestmark = [
    pytest.mark.platforms("posix"),
    pytest.mark.slow_test(
        300, reason="RED on the fd-scan guard: 3 hold() calls x 12-28 s each under 12 GIL-busy threads"),
]

_SCRATCH_FDS = 500
_CHURN_THREADS = 12
_BUDGET_S = 0.050
_FLOOR_MULTIPLE = 20


def test_hold_is_constant_in_fd_count_under_gil_load(tmp_path, monkeypatch):
    if not lg.supported():
        pytest.skip("no OFD locks on this runtime")
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    assert os.path.exists(f"{db}-shm")
    listed = []
    real_listdir = os.listdir

    def _counting_listdir(path="."):
        listed.append(path)
        return real_listdir(path)

    fds = [os.open(os.devnull, os.O_RDONLY) for _ in range(_SCRATCH_FDS)]
    stop = threading.Event()
    blob = {"k%d" % i: [i, str(i) * 8, {"n": i}] for i in range(200)}

    def _churn():
        while not stop.is_set():
            json.loads(json.dumps(blob))

    threads = [threading.Thread(target=_churn, daemon=True) for _ in range(_CHURN_THREADS)]
    timings, floor = [], []
    try:
        for t in threads:
            t.start()
        time.sleep(0.2)
        for _ in range(15):
            t0 = time.perf_counter()
            os.stat(db)
            floor.append(time.perf_counter() - t0)
        monkeypatch.setattr(os, "listdir", _counting_listdir)
        for _ in range(3):
            t0 = time.perf_counter()
            held = lg.hold(db)
            timings.append(time.perf_counter() - t0)
            assert held, "hold() armed nothing on a live WAL database"
            lg.release(held)  # retire() (the close path, may scan) is not part of hold()
        monkeypatch.setattr(os, "listdir", real_listdir)
    finally:
        stop.set()
        for t in threads:
            t.join()
        for fd in fds:
            os.close(fd)
        conn.close()
    syscall_floor = statistics.median(floor)
    budget = _BUDGET_S + _FLOOR_MULTIPLE * syscall_floor
    print("hold() timings (s):", ["%.4f" % s for s in timings],
          "syscall floor %.4f s, budget %.4f s" % (syscall_floor, budget))
    assert not listed, f"hold() enumerated the fd table: {listed}"
    assert all(s < budget for s in timings), f"hold() not O(1) under GIL load: {timings} (budget {budget:.3f}s)"
