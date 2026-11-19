"""REGRESSION: an lcm ingest batch must not write SQLite statement journals to temp files (t_ba5a93d0).

``_append_protected_batch`` inserts a batch inside one transaction. Each INSERT fires the
msg_fts_insert trigger, so SQLite opens a statement journal for it and copies every existing
page the FTS5 flush touches into it. The journal spills to an etilqs_* file past 64 KiB unless
temp_store=MEMORY. On the Studio that was ~10.6 GB/day from the default gateway (dtrace,
t_d24713d3); replaying 3,000 real rows on a copy of the 14 GB lcm.db wrote 125 MB of temp
files, and the largest single statement journal was 0.44 MB.

Detection: SQLite creates temp files in ``PRAGMA temp_store_directory`` and unlinks them at
once, so the test watches that directory's mtime (both macOS and Linux bump it on create/unlink).
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from plugins.context_engine.lcm.store import MessageStore


def _words(seed: int, n: int = 20) -> str:
    return " ".join(f"w{seed}x{i}" for i in range(n))


def _seed(st: MessageStore) -> None:
    for b in range(20):
        st.append_batch("seed", [{"role": "user", "content": _words(b * 100 + i)} for i in range(50)])


def _batch() -> list[dict]:
    # Each INSERT in the batch flushes one more FTS5 segment, so the structure record every
    # statement rewrites keeps growing: 200 rows push the statement journal past 64 KiB.
    return [{"role": "assistant", "content": _words(10_000 + i)} for i in range(200)]


@pytest.fixture
def temp_dir_probe(tmp_path):
    """Point SQLite's (process-global) temp directory at an empty dir; yield a 'created a temp file?' check."""
    probe = tmp_path / "sqlite-tmp"
    probe.mkdir()
    con = sqlite3.connect(":memory:")
    try:
        con.execute(f"PRAGMA temp_store_directory = '{probe}'")
    except sqlite3.DatabaseError:  # pragma: no cover - SQLITE_OMIT_DEPRECATED build
        pytest.skip("this SQLite build has no temp_store_directory pragma")

    def created_since_reset() -> bool:
        return os.stat(probe).st_mtime_ns != 0

    def reset() -> None:
        os.utime(probe, ns=(0, 0))

    created_since_reset.reset = reset
    try:
        yield created_since_reset
    finally:
        con.execute("PRAGMA temp_store_directory = ''")
        con.close()


def test_ingest_batch_writes_no_temp_files(tmp_path, temp_dir_probe):
    st = MessageStore(db_path=str(tmp_path / "lcm.db"))
    try:
        _seed(st)
        temp_dir_probe.reset()
        ids = st.append_batch("s", _batch())
        assert len(ids) == 200
        assert not temp_dir_probe(), "ingest batch spilled a statement journal to a temp file"
        assert st._conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        hits = st._conn.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'w10199x19'"
        ).fetchone()[0]
        assert hits == 1
    finally:
        st.close()


def test_probe_sees_a_spill(tmp_path, temp_dir_probe):
    """The probe is not blind: the same batch on a temp_store=FILE connection does create a temp file."""
    st = MessageStore(db_path=str(tmp_path / "lcm.db"))
    try:
        st._conn.execute("PRAGMA temp_store = FILE")
        _seed(st)
        temp_dir_probe.reset()
        st.append_batch("s", _batch())
        assert temp_dir_probe()
    finally:
        st.close()
