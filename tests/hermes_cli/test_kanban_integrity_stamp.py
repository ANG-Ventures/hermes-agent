"""Cross-process healthy verdict for the kanban connect-time integrity guard.

Regression for t_5e8801d8: every new CLI process ran a full ``PRAGMA
integrity_check`` on the 740 MB board while holding the cross-process init
lock (``_INITIALIZED_PATHS`` is per-process), so concurrent callers queued
behind each other's scans and hit the 10 s init-lock timeout. A clean check now
leaves a stamp bound to the DB file (inode, birth time, boot, size floor); a
later process within the TTL trusts it, anything else re-runs the full check.

Counts come from a ``_run_integrity_check`` shim in real subprocesses, never a
wall-clock bound.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

_REPO = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from hermes_cli import kanban_db_connect as kbc

    db, counter, go = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    original = kbc._run_integrity_check

    def counted(conn):
        with counter.open("a") as fh:
            fh.write("check\\n")
        return original(conn)

    kbc._run_integrity_check = counted
    deadline = time.monotonic() + 120
    while not go.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    kbc.connect(db_path=db).close()
    """
)


def _stamp(db: Path) -> Path:
    return db.with_name(db.name + ".integrity-ok")


def _seed(db: Path) -> None:
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    kb.init_db(db_path=db)
    with kbc.connect_closing(db_path=db) as conn:
        kb.create_task(conn, title="seed")
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))


def _spawn_connects(db: Path, n: int, tmp: Path, env_extra: dict | None = None) -> int:
    """Run ``n`` concurrent fresh-process ``connect()`` calls; return the
    total number of full integrity checks they ran."""
    counter = tmp / f"checks-{time.monotonic_ns()}.log"
    go = tmp / f"go-{time.monotonic_ns()}"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO)
    env.update(env_extra or {})
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(db), str(counter), str(go)],
            cwd=str(_REPO), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(n)
    ]
    go.touch()
    for proc in procs:
        out, err = proc.communicate(timeout=240)
        assert proc.returncode == 0, err[-2000:]
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def test_concurrent_fresh_processes_run_one_full_check(tmp_path):
    db = tmp_path / "kanban.db"
    _seed(db)
    _stamp(db).unlink(missing_ok=True)  # start with no verdict on disk

    assert _spawn_connects(db, 6, tmp_path) == 1


def test_second_process_within_ttl_takes_stamp_path(tmp_path):
    db = tmp_path / "kanban.db"
    _seed(db)
    _stamp(db).unlink(missing_ok=True)

    assert _spawn_connects(db, 1, tmp_path) == 1
    assert _spawn_connects(db, 1, tmp_path) == 0


def test_inode_change_reruns_full_check(tmp_path):
    db = tmp_path / "kanban.db"
    _seed(db)
    assert _spawn_connects(db, 1, tmp_path) <= 1  # stamp now present
    copy = tmp_path / "kanban.db.copy"
    shutil.copy2(db, copy)
    os.replace(copy, db)  # same bytes, new inode

    assert _spawn_connects(db, 1, tmp_path) == 1


def _corrupt_bytes(size: int) -> bytes:
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    return (header.ljust(100, b"\x00") + b"not a page \x00\x01" * (size // 13 + 1))[:size]


def _grow_and_stamp(db: Path, tmp_path: Path) -> int:
    """Grow the board past its seed size, then leave a fresh stamp for the
    grown file written by a real fresh process. Returns the grown size."""
    with kbc.connect_closing(db_path=db) as conn:
        conn.execute("CREATE TABLE filler(x BLOB)")
        conn.executemany("INSERT INTO filler VALUES (zeroblob(4096))", [()] * 256)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    _stamp(db).unlink(missing_ok=True)  # drop the stamp this process wrote
    assert _spawn_connects(db, 1, tmp_path) == 1
    assert _stamp(db).exists()
    return db.stat().st_size


def _rewrite_in_place(db: Path, data: bytes) -> None:
    inode = db.stat().st_ino
    for sidecar in ("-wal", "-shm"):
        Path(str(db) + sidecar).unlink(missing_ok=True)
    with db.open("r+b") as fh:  # truncate + rewrite: same inode, like `cp x kanban.db`
        fh.truncate(0)
        fh.write(data)
    assert db.stat().st_ino == inode


def test_same_inode_restore_of_older_copy_reruns_full_check(tmp_path):
    """Prism ed5edb9798ea: ``cp backup kanban.db`` keeps the inode."""
    db = tmp_path / "kanban.db"
    _seed(db)
    backup = (tmp_path / "backup.db")
    shutil.copyfile(db, backup)
    _grow_and_stamp(db, tmp_path)
    _rewrite_in_place(db, backup.read_bytes())

    assert _spawn_connects(db, 1, tmp_path) == 1


def test_db_corrupted_in_place_under_fresh_stamp_quarantines(tmp_path):
    """Prism ed5edb9798ea: a damaged file on the SAME inode under a fresh
    stamp must still reach the full check and be quarantined."""
    db = tmp_path / "kanban.db"
    _seed(db)
    grown = _grow_and_stamp(db, tmp_path)
    _rewrite_in_place(db, _corrupt_bytes(grown // 2))
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))

    with pytest.raises(kbc.KanbanDbCorruptError):
        kbc.connect(db_path=db)
    assert list(tmp_path.glob("kanban.db.corrupt.*.bak"))


def test_stamp_from_another_boot_reruns_full_check(tmp_path):
    """A torn page from a power loss keeps the inode; a reboot voids the stamp."""
    db = tmp_path / "kanban.db"
    _seed(db)
    assert _spawn_connects(db, 1, tmp_path) <= 1
    stamp = json.loads(_stamp(db).read_text())
    stamp["boot"] = "a-previous-boot"
    _stamp(db).write_text(json.dumps(stamp))

    assert _spawn_connects(db, 1, tmp_path) == 1


def test_expired_stamp_reruns_full_check(tmp_path):
    db = tmp_path / "kanban.db"
    _seed(db)
    assert _spawn_connects(db, 1, tmp_path) <= 1
    stamp = json.loads(_stamp(db).read_text())
    stamp["checked_at"] -= 3600
    _stamp(db).write_text(json.dumps(stamp))

    assert _spawn_connects(db, 1, tmp_path) == 1


def test_ttl_zero_disables_stamp(tmp_path):
    db = tmp_path / "kanban.db"
    _seed(db)
    env = {kbc._INTEGRITY_STAMP_TTL_ENV: "0"}
    assert _spawn_connects(db, 1, tmp_path, env) == 1
    assert _spawn_connects(db, 1, tmp_path, env) == 1


def test_corrupt_db_quarantines_despite_fresh_stamp_from_other_inode(tmp_path):
    db = tmp_path / "kanban.db"
    _seed(db)
    assert _spawn_connects(db, 1, tmp_path) <= 1
    assert _stamp(db).exists()
    corrupt = tmp_path / "corrupt.tmp"
    corrupt.write_bytes(_corrupt_bytes(100 + 13 * 256))
    for sidecar in ("-wal", "-shm"):
        Path(str(db) + sidecar).unlink(missing_ok=True)
    os.replace(corrupt, db)
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))

    with pytest.raises(kbc.KanbanDbCorruptError):
        kbc.connect(db_path=db)
    assert list(tmp_path.glob("kanban.db.corrupt.*.bak"))
