"""One transient read error must not start the corrupt-board quarantine copy (t_3200d600).

The quarantine copy raw-opens/reads/closes the board, its -wal and -shm. In a process holding a
connection that ``close()`` cancels the connection's POSIX locks, which t_e87a3e20 showed turns a
healthy board into a corrupt one. A short read zero-fills the page, so ``integrity_check`` reports
it as a damaged page (rows, not an exception) and the guard used to copy on that one read. The
verdict now needs a second, agreeing read on a fresh connection. (Holding the board offline during
the copy is ``database_offline``, #428, tested there.)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc

pytestmark = pytest.mark.platforms("posix")


def _board(tmp_path: Path) -> tuple[Path, int, int]:
    """A small rollback-journal DB; returns (path, page_size, page number of t's root)."""
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE t(v TEXT)")
    conn.execute("CREATE INDEX t_v ON t(v)")
    conn.executemany("INSERT INTO t VALUES (?)", [(f"row-{i:05d}" * 4,) for i in range(400)])
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='t'").fetchone()[0]
    conn.close()
    return db, page_size, root


def _zero_page(db: Path, page_size: int, pgno: int) -> bytes:
    with db.open("r+b") as fh:
        fh.seek((pgno - 1) * page_size)
        saved = fh.read(page_size)
        fh.seek((pgno - 1) * page_size)
        fh.write(b"\x00" * page_size)
    return saved


def _restore_page(db: Path, page_size: int, pgno: int, saved: bytes) -> None:
    with db.open("r+b") as fh:
        fh.seek((pgno - 1) * page_size)
        fh.write(saved)


@pytest.fixture
def transient_zero_page(tmp_path, monkeypatch):
    """The first integrity probe reads a really zeroed page; the bytes are back before the next."""
    db, page_size, pgno = _board(tmp_path)
    real_probe = kbc._probe_integrity
    calls: list[object] = []

    def probe(path):
        if calls:
            calls.append(real_probe(path))
            return calls[-1]
        saved = _zero_page(db, page_size, pgno)
        try:
            calls.append(real_probe(path))
        except sqlite3.DatabaseError as exc:  # a damaged page can also raise instead of reporting
            calls.append(exc)
            raise
        finally:
            _restore_page(db, page_size, pgno, saved)
        return calls[-1]

    monkeypatch.setattr(kbc, "_probe_integrity", probe)
    monkeypatch.setattr(kbc, "_CORRUPT_CONFIRM_DELAY_SECONDS", 0.0, raising=False)
    monkeypatch.setenv(kbc._INTEGRITY_STAMP_TTL_ENV, "0")
    return db, calls


def test_one_transient_bad_integrity_read_does_not_quarantine(transient_zero_page, tmp_path):
    db, calls = transient_zero_page
    kbc._guard_existing_db_is_healthy(db)
    assert len(calls) == 2
    # The first read really saw damage (an error, or non-ok integrity rows); the second is clean.
    assert isinstance(calls[0], sqlite3.DatabaseError) or not kbc._integrity_messages_ok(calls[0])
    assert kbc._integrity_messages_ok(calls[1])
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))


@pytest.mark.parametrize("first_rows", [
    ["*** in database main ***\nPage 5: unable to get the page. error code=522"],  # IOERR mid-check
    ["row 7 missing from index t_v"],  # index-only: used to copy AND run REINDEX on one read
])
def test_one_transient_bad_integrity_row_set_does_not_copy_or_reindex(tmp_path, monkeypatch, first_rows):
    db, _, _ = _board(tmp_path)
    real_probe = kbc._probe_integrity
    calls: list[list[str]] = []
    reindexed: list[object] = []

    def probe(path):
        calls.append(list(first_rows) if not calls else real_probe(path))
        return calls[-1]

    monkeypatch.setattr(kbc, "_probe_integrity", probe)
    monkeypatch.setattr(kbc, "_attempt_index_reindex_repair", lambda *a: reindexed.append(a) or (True, ["ok"]))
    monkeypatch.setattr(kbc, "_CORRUPT_CONFIRM_DELAY_SECONDS", 0.0, raising=False)
    monkeypatch.setenv(kbc._INTEGRITY_STAMP_TTL_ENV, "0")
    kbc._guard_existing_db_is_healthy(db)
    assert len(calls) == 2 and not reindexed
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))


def test_repair_db_does_not_copy_on_one_transient_bad_read(transient_zero_page, tmp_path):
    db, calls = transient_zero_page
    result = kbc.repair_db(db)
    assert result.status == "ok" and result.backup_path is None
    assert len(calls) == 2
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))


def test_corruption_both_reads_agree_on_is_still_quarantined(tmp_path, monkeypatch):
    db, page_size, pgno = _board(tmp_path)
    _zero_page(db, page_size, pgno)
    monkeypatch.setattr(kbc, "_CORRUPT_CONFIRM_DELAY_SECONDS", 0.0, raising=False)
    monkeypatch.setenv(kbc._INTEGRITY_STAMP_TTL_ENV, "0")
    with pytest.raises(kbc.KanbanDbCorruptError) as info:
        kbc._guard_existing_db_is_healthy(db)
    assert info.value.backup_path is not None and info.value.backup_path.is_file()
    assert len(list(tmp_path.glob("kanban.db.corrupt.*.bak"))) == 1
