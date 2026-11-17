"""The dispatcher leader's board anchor (t_1460fa32) and the symlinked-db memo key (Prism
558b83fbb2b9 on #405).

t_2cfa2782 measured that a read-only open+close of an idle WAL board, when no other process holds
it, rebuilds the board's -shm on disk (ftruncate + fsync on close). One idle connection held by
another process stops that. These tests pin what the anchor must and must not do.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from hermes_cli import kanban_board_anchor as kba
from hermes_cli import kanban_db as kb
from hermes_cli import sqlite_quiet_memo as memo


def _wal_db(path, rows=3):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=wal")
    c.execute("CREATE TABLE t (x INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    c.commit()
    c.close()
    return path


_OPEN_CLOSE = (
    "import sqlite3,sys\n"
    "c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)\n"
    "c.execute('SELECT COUNT(*) FROM t').fetchone()\n"
    "c.close()\n"
)


def _other_process_open_close(path) -> None:
    subprocess.run([sys.executable, "-c", _OPEN_CLOSE, str(path)], check=True, timeout=60)


def _shm_stamp(path):
    st = os.stat(str(path) + "-shm")
    return (st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def test_another_process_does_not_rebuild_the_shm_while_anchored(tmp_path):
    db = _wal_db(tmp_path / "board.db")
    # Control: with nobody holding the board, an opener is first and rebuilds the -shm.
    _other_process_open_close(db)
    before = _shm_stamp(db)
    time.sleep(0.05)
    _other_process_open_close(db)
    assert _shm_stamp(db) != before, "control: an unanchored open should rebuild the -shm"

    anchor = kba.BoardAnchor(lambda: [db], checkpoint=False)
    try:
        assert anchor.refresh()["held"] == 1
        before = _shm_stamp(db)
        for _ in range(3):
            time.sleep(0.05)
            _other_process_open_close(db)
        assert _shm_stamp(db) == before
    finally:
        anchor.close_all()


def test_anchor_holds_no_lock_a_writer_or_checkpoint_needs(tmp_path):
    db = _wal_db(tmp_path / "board.db")
    anchor = kba.BoardAnchor(lambda: [db], checkpoint=False)
    try:
        anchor.refresh()
        w = sqlite3.connect(db, timeout=0)
        w.execute("INSERT INTO t VALUES (7)")
        w.commit()
        busy, _log, _ckpt = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        w.close()
        assert busy == 0
        assert os.path.getsize(str(db) + "-wal") == 0
    finally:
        anchor.close_all()


def _fds_on(path) -> int:
    real = os.path.realpath(path)
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(real):
            n += 1
    return n


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_three_fds_per_board_and_none_left_after_unlink(tmp_path):
    dbs = [_wal_db(tmp_path / f"b{i}.db") for i in range(2)]
    anchor = kba.BoardAnchor(lambda: list(dbs), checkpoint=False)
    try:
        anchor.refresh()
        assert [_fds_on(d) for d in dbs] == [3, 3]
        for suffix in ("", "-wal", "-shm"):
            p = str(dbs[0]) + suffix
            if os.path.exists(p):
                os.unlink(p)
        assert anchor.refresh()["closed"] == 1
        assert anchor.held_paths() == [str(dbs[1])]
        assert _fds_on(dbs[0]) == 0  # no fd keeps the deleted inode alive
    finally:
        anchor.close_all()
    assert _fds_on(dbs[1]) == 0


def test_reopens_on_a_replaced_file_and_follows_the_board_list(tmp_path):
    a = _wal_db(tmp_path / "a.db")
    boards = [a]
    anchor = kba.BoardAnchor(lambda: list(boards), rescan_every=1, checkpoint=False)
    try:
        anchor.refresh()
        old = anchor._held[str(a)][0]
        _wal_db(tmp_path / "new.db", rows=9)
        os.replace(tmp_path / "new.db", a)
        r = anchor.refresh()
        assert (r["closed"], r["opened"]) == (1, 1)
        assert anchor._held[str(a)][0] != old
        assert anchor._held[str(a)][1].execute("SELECT COUNT(*) FROM t").fetchone()[0] == 9
        b = _wal_db(tmp_path / "b.db")
        boards.append(b)
        assert anchor.refresh()["opened"] == 1
        boards.remove(a)
        assert anchor.refresh()["closed"] == 1
        assert anchor.held_paths() == [str(b)]
    finally:
        anchor.close_all()


def _grow_wal(db, n=400):
    w = sqlite3.connect(db)
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("PRAGMA journal_size_limit=-1")
    for i in range(n):
        w.execute("INSERT INTO t VALUES (?)", (i,))
        w.commit()
    return w


def test_wal_is_truncated_past_the_cap_and_after_a_quiet_spell_but_never_waited_for(tmp_path):
    db = _wal_db(tmp_path / "board.db")
    wal = str(db) + "-wal"
    anchor = kba.BoardAnchor(lambda: [db], wal_cap=64 * 1024, quiet_s=3600)
    try:
        anchor.refresh()
        w = _grow_wal(db)
        assert os.path.getsize(wal) > 64 * 1024
        assert anchor.refresh()["truncated"] == 1
        assert os.path.getsize(wal) == 0
        # Small and recent: left alone. Small and quiet: truncated.
        w.execute("INSERT INTO t VALUES (1)")
        w.commit()
        assert 0 < os.path.getsize(wal) < 64 * 1024
        assert anchor.refresh()["truncated"] == 0
        anchor.quiet_s = 30
        old = time.time() - 60
        os.utime(wal, (old, old))
        assert anchor.refresh()["truncated"] == 1
        assert os.path.getsize(wal) == 0
        # A reader inside a transaction: the checkpoint reports busy at once, nothing waits.
        w.execute("INSERT INTO t VALUES (2)")
        w.commit()
        os.utime(wal, (old, old))
        r = sqlite3.connect(db)
        r.execute("BEGIN")
        r.execute("SELECT COUNT(*) FROM t").fetchone()
        t0 = time.monotonic()
        assert anchor.refresh()["truncated"] == 0
        assert time.monotonic() - t0 < 1.0
        assert os.path.getsize(wal) > 0
        r.rollback()
        r.close()
        w.close()
    finally:
        anchor.close_all()


def test_thread_start_stop_and_off_switch(tmp_path, monkeypatch):
    db = _wal_db(tmp_path / "board.db")
    anchor = kba.BoardAnchor(lambda: [db], interval=0.5, checkpoint=False).start()
    deadline = time.time() + 5
    while not anchor.held_paths() and time.time() < deadline:
        time.sleep(0.05)
    assert anchor.held_paths() == [str(db)]
    anchor.stop()
    assert anchor.held_paths() == []
    assert not any(t.name == "kanban-board-anchor" for t in threading.enumerate())

    monkeypatch.delenv(kba.ENV_SWITCH, raising=False)
    assert kba.enabled({}) and kba.enabled({"board_anchor": True})
    assert not kba.enabled({"board_anchor": False})
    monkeypatch.setenv(kba.ENV_SWITCH, "0")
    assert not kba.enabled({})
    assert kba.start_if_enabled({}) is None


def test_symlinked_board_reads_its_targets_wal_not_a_stale_memo(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_SQLITE_QUIET_MEMO", raising=False)
    memo.clear()
    target = tmp_path / "real" / "kanban.db"
    target.parent.mkdir()
    kb.init_db(target)
    old = time.time() - 3600
    os.utime(target, (old, old))
    link = tmp_path / "kanban.db"
    link.symlink_to(target)
    try:
        assert kb._board_db_is_empty(link) is True
        assert kb._board_db_is_empty(link) is True  # remembered
        w = sqlite3.connect(target)  # held open: the row stays in the target's -wal
        w.execute("PRAGMA wal_autocheckpoint=0")
        # Fill every NOT NULL column without a default, whatever the schema version.
        need = {r[1]: r[2] for r in w.execute("PRAGMA table_info(tasks)")
                if r[3] and r[4] is None and not r[5]}
        need["id"] = "TEXT"
        vals = [0 if "INT" in (t or "").upper() else "t_sym" for t in need.values()]
        w.execute(f"INSERT INTO tasks ({','.join(need)}) VALUES ({','.join('?' * len(need))})",
                  vals)
        w.commit()
        assert os.path.getsize(str(target) + "-wal") > 0
        assert kb._board_db_is_empty(link) is False
        w.close()
    finally:
        memo.clear()


def test_standalone_daemon_holds_the_anchor_while_it_runs(monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd

    events = []

    class _Fake:
        def stop(self, timeout=5.0):
            events.append("stop")

    def _start(cfg=None):
        events.append("start")
        return _Fake()

    monkeypatch.setattr(kba, "start_if_enabled", _start)
    stop = threading.Event()
    stop.set()  # no tick: start, loop exits at once, stop
    kbd.run_daemon(interval=1.0, stop_event=stop)
    assert events == ["start", "stop"]


def test_anchor_board_list_has_live_and_phantom_boards_never_archived(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    kb.create_board("alpha")
    kb.create_board("beta")
    kb.init_db(kb.kanban_db_path("beta"))
    kb.remove_board("beta", archive=True)  # -> boards/_archived/beta-<ts>/
    ghost = kb.boards_root() / "ghost"
    ghost.mkdir()
    kb.init_db(ghost / "kanban.db")  # kanban.db, 0 tasks, no board.json: a phantom
    assert any((kb.boards_root() / "_archived").iterdir())

    opened = []
    real_connect = sqlite3.connect

    def counting_connect(*a, **k):
        opened.append(str(a[0] if a else k.get("database")))
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    paths = kba.live_board_db_paths()
    # Live boards and the phantom (list_boards opens it once per process to prove it empty);
    # nothing under _archived.
    want = [Path(os.path.realpath(kb.kanban_db_path(s))) for s in ("default", "alpha")]
    want.append(Path(os.path.realpath(ghost / "kanban.db")))
    assert sorted(paths) == sorted(want)
    assert [m["slug"] for m in kb.list_boards(include_archived=False)] == ["default", "alpha"]
    assert not [p for p in opened if "_archived" in p]
