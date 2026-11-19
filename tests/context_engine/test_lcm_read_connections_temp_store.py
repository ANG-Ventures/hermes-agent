"""REGRESSION: LCM read connections must keep SQLite temp B-trees in memory (t_188b19e8).

lcm_grep's deadline-bounded readers (tools.py ``_lcm_grep_full_text_with_deadline``, used by hybrid and
semantic mode) and lcm_recall's summary-source reader opened their own read-only connections with the
default temp_store. The same search sorts as ``MessageStore.search`` then wrote 0.1-8.2 GB of etilqs_*
temp files per call on a copy of the Studio's 14 GB lcm.db (t_ba5a93d0 fixed only MessageStore's own
connection). Every read connection now goes through ``sqlite_util.connect_read``.

Detection for the behaviour test: SQLite creates temp files in ``PRAGMA temp_store_directory`` and
unlinks them at once, so the test watches that directory's mtime (as in test_lcm_ingest_no_temp_spill).
"""
from __future__ import annotations

import ast
import collections
import os
import sqlite3
import time
from pathlib import Path

import pytest

from plugins.context_engine.lcm import tools
from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every raw ``sqlite3.connect(`` left in plugins/context_engine/lcm, with how many per function. These are
# writers, in-memory scratch DBs and backup destinations; a READ connection belongs in
# ``sqlite_util.connect_read`` instead. A new raw connect fails the contract until it is either routed
# through the helper or listed here with the reason it is not a read connection.
_NOT_READ_CONNECTIONS = {
    ("assertion_store.py", "_open_connection"): (1, "read-write branch; the read_only branch uses connect_read"),
    ("command.py", "_doctor_repair_schema_stamp_apply_text"): (1, "doctor repair writer"),
    ("dag.py", "_init_db"): (1, "SummaryDAG writer connection"),
    ("db_bootstrap.py", "_expected_assertion_schema_contract"): (1, "in-memory scratch DB"),
    ("db_bootstrap.py", "verify_temporal_rollup_schema"): (1, "in-memory scratch DB"),
    ("db_bootstrap.py", "_run_background_integrity_scan"): (3, "scan stamp / meta / cleanup writers"),
    ("db_bootstrap.py", "_dispatch_background_integrity_scan"): (1, "claim row writer"),
    ("db_bootstrap.py", "_run_background_fts_rebuild"): (1, "FTS rebuild writer"),
    ("lifecycle_state.py", "_init_db"): (1, "lifecycle writer connection"),
    ("maintenance.py", "backup_database"): (1, "backup destination"),
    ("maintenance.py", "rotate_backup_database"): (1, "backup destination"),
    ("query_view_store.py", "__init__"): (1, "query view writer connection"),
    ("rollup_store.py", "_init_db"): (1, "rollup writer connection"),
    ("sqlite_util.py", "connect_read"): (1, "the read helper itself"),
    ("store.py", "_init_db"): (1, "MessageStore writer; sets temp_store=MEMORY itself (t_ba5a93d0)"),
    ("trajectory_store.py", "_open_connection"): (1, "read-write branch; the read_only branch uses connect_read"),
    ("trajectory_store.py", "backup_to"): (1, "backup destination"),
    ("vector_store.py", "_init_db"): (1, "vector store writer connection"),
}


def _raw_connects(root: Path) -> collections.Counter:
    lcm = root / "plugins" / "context_engine" / "lcm"
    found: collections.Counter = collections.Counter()
    for path in sorted(lcm.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "connect" and getattr(node.func.value, "id", "") == "sqlite3"):
                continue
            func = node
            while func in parents and not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = parents[func]
            found[(str(path.relative_to(lcm)), getattr(func, "name", "<module>"))] += 1
    return found


def test_every_lcm_read_connection_goes_through_connect_read():
    want = {key: count for key, (count, _why) in _NOT_READ_CONNECTIONS.items()}
    assert dict(_raw_connects(REPO_ROOT)) == want


def test_contract_catches_a_raw_read_connect(tmp_path):
    """The contract is not vacuous: a raw read-only connect in a new module is reported."""
    fake = tmp_path / "plugins" / "context_engine" / "lcm"
    fake.mkdir(parents=True)
    (fake / "new_reader.py").write_text(
        "import sqlite3\ndef search(p):\n    return sqlite3.connect(f'file:{p}?mode=ro', uri=True)\n",
        encoding="utf-8",
    )
    assert _raw_connects(tmp_path) == {("new_reader.py", "search"): 1}


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


@pytest.fixture
def seeded_engine(tmp_path):
    eng = LCMEngine(config=LCMConfig(database_path=str(tmp_path / "lcm.db")), hermes_home=str(tmp_path))
    pad = "lorem ipsum dolor " * 120
    for b in range(10):
        eng._store.append_batch(
            f"s{b}", [{"role": "user", "content": f"spillword row {b}-{i} {pad}"} for i in range(400)])
    yield eng
    eng.shutdown()


def _grep(engine, sort):
    return tools._lcm_grep_full_text_with_deadline(
        {"query": "spillword", "sort": sort, "limit": 250, "session_scope": "all"}, engine=engine, deadline=time.monotonic() + 60)


@pytest.mark.parametrize("sort", ["recency", "relevance"])
def test_deadline_grep_reader_writes_no_temp_files(seeded_engine, temp_dir_probe, sort):
    temp_dir_probe.reset()
    payload = _grep(seeded_engine, sort)
    assert "error" not in payload, payload
    assert len(payload["results"]) == 200  # the tool caps limit at 200
    assert not temp_dir_probe(), f"lcm_grep ({sort}) read connection spilled a sort to a temp file"


def test_probe_sees_a_read_spill(seeded_engine, temp_dir_probe, monkeypatch):
    """The probe is not blind: the same grep on temp_store=FILE read connections does create a temp file."""
    real = sqlite3.connect

    def file_connect_read(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.execute("PRAGMA temp_store = FILE")
        return conn

    monkeypatch.setattr(tools, "connect_read", file_connect_read)
    temp_dir_probe.reset()
    _grep(seeded_engine, "recency")
    assert temp_dir_probe()
