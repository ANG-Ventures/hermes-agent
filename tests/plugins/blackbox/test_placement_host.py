"""Blackbox ``placement_host`` column: which box ran a kanban worker's tools (t_c9fc6c9f).

- the column is a guarded additive migration (old DB gains it, re-open is a no-op);
- a dispatcher-owned worker turn carries its pool host, ``studio-local`` when unplaced;
- a non-worker turn, and a child process that merely inherited a worker's env, stay NULL;
- orphan repair (another process writing the row) never stamps its own host;
- the backfill script is dry-run by default and never writes without ``--apply``.
"""
from __future__ import annotations

import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import plugins.blackbox as blackbox
from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord
from hermes_cli import kanban_worker_hosts as kwh

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "blackbox" / "turns.db"
    with patch("plugins.blackbox.store._db_path", return_value=path):
        yield path


@pytest.fixture
def no_worker_env(monkeypatch):
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_OWNER_PID", kwh.PLACEMENT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    # Local backend baseline, independent of an inherited TERMINAL_ENV or a
    # config bridge; remote-backend cases override it explicitly.
    from tools import terminal_tool

    monkeypatch.setattr(terminal_tool, "_ensure_terminal_env_bridged", lambda: None)
    monkeypatch.setenv("TERMINAL_ENV", "local")


def _columns(path):
    conn = sqlite3.connect(str(path))
    try:
        return [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(turns)")]
    finally:
        conn.close()


def _host(path, turn_id):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT placement_host FROM turns WHERE turn_id = ? OR turn_id LIKE ?",
            (turn_id, "%:task:" + turn_id),
        ).fetchone()[0]
    finally:
        conn.close()


def _build(**kwargs):
    return blackbox._build_record(
        session_id="s1", interrupted=False, model="m", platform="cli", provider="p",
        user_message="u", final_response="f", turn_usage={}, cfg={}, kwargs=kwargs,
    )


def _worker(monkeypatch, *, placement_host=None, owner_pid=None):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_KANBAN_OWNER_PID", str(owner_pid or os.getpid()))
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", placement_host)
    if placement_host:
        monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps(
            {"host": placement_host, "env": {"TERMINAL_ENV": "ssh"}}))


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #
def test_new_db_has_nullable_text_column(db):
    store._connect().close()
    assert ("placement_host", "TEXT") in _columns(db)


def test_old_db_gains_column_and_reopen_is_idempotent(db, no_worker_env):
    # A store written by the previous release: today's schema minus the column.
    store.insert_turn(TurnRecord(turn_id="old", profile="default"))
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE turns DROP COLUMN placement_host")
    conn.commit()
    conn.close()
    assert "placement_host" not in [c for c, _ in _columns(db)]

    store._connect().close()
    store._connect().close()

    names = [c for c, _ in _columns(db)]
    assert names.count("placement_host") == 1
    assert _host(db, "old") is None


# --------------------------------------------------------------------------- #
# population
# --------------------------------------------------------------------------- #
def test_spilled_worker_turn_carries_pool_host(db, no_worker_env, monkeypatch):
    _worker(monkeypatch, placement_host="ci-box")
    rec = _build(turn_id="turn_w")
    assert rec.placement_host == "ci-box"
    store.insert_turn(rec)
    assert _host(db, "turn_w") == "ci-box"


def test_failed_placement_reapply_suppresses_live_host(db, no_worker_env, monkeypatch):
    _worker(monkeypatch, placement_host="ci-box")
    monkeypatch.setattr(kwh, "_reapply", {"failed": 0, "warned": False})
    # Exercise the real failure path without writing to a live kanban board.
    monkeypatch.setattr(kwh, "_record_reapply_failure", lambda count: None)
    assert _build().placement_host == "ci-box"
    assert kwh.reapply_or_record("terminal", environ={}) is None
    assert kwh.reapply_failures() == 1
    store.insert_turn(_build(turn_id="failed_reapply"))
    assert _host(db, "failed_reapply") is None
    # Recovery cannot make this mixed-host run safe to attribute retroactively.
    assert kwh.reapply_or_record("terminal", environ={
        kwh.PLACEMENT_ENV: json.dumps({
            "host": "ci-box", "env": {"TERMINAL_ENV": "ssh"},
        }),
    }) == "ci-box"
    assert _build().placement_host is None


@pytest.mark.parametrize("applied, expected", [("ace-media", None), ("ci-box", "ci-box")])
def test_successful_reapply_must_match_boot_host(db, no_worker_env, monkeypatch, applied, expected):
    # Prism 2f2377ff612f: a reapply that SUCCEEDS onto another host moved the
    # tools off the boot placement; the boot host is no longer the actual host.
    _worker(monkeypatch, placement_host="ci-box")
    monkeypatch.setattr(kwh, "_reapply", {"failed": 0, "warned": False})
    recorded = []
    monkeypatch.setattr(kwh, "_record_reapply_failure", recorded.append)
    environ = {kwh.PLACEMENT_ENV: json.dumps({
        "host": applied, "env": {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": applied},
    })}
    assert kwh.reapply_or_record("terminal", environ=environ) == applied
    assert environ["TERMINAL_SSH_HOST"] == applied  # the tool call really targets it
    assert recorded == ([] if expected else [1])  # run metadata carries the mismatch
    store.insert_turn(_build(turn_id="reapplied_" + applied))
    assert _host(db, "reapplied_" + applied) == expected


def test_boot_record_wins_over_mutated_env(no_worker_env, monkeypatch):
    _worker(monkeypatch, placement_host="ace-media")
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", "ci-box")
    assert _build().placement_host == "ci-box"


def test_unplaced_worker_turn_is_studio_local(db, no_worker_env, monkeypatch):
    _worker(monkeypatch)
    store.insert_turn(_build(turn_id="turn_l"))
    assert _host(db, "turn_l") == kwh.LOCAL_HOST_NAME == "studio-local"


def test_non_worker_turn_is_null(db, no_worker_env):
    store.insert_turn(_build(turn_id="turn_n"))
    assert _host(db, "turn_n") is None


def test_child_that_inherited_worker_env_is_null(no_worker_env, monkeypatch):
    # The grant names another pid: this process is not the worker.
    _worker(monkeypatch, placement_host="ci-box", owner_pid=os.getpid() + 100000)
    assert _build().placement_host is None


@pytest.mark.parametrize("context_name", ["delegated_child_context", "non_dispatcher_owned_context"])
def test_in_process_non_worker_context_is_null(no_worker_env, monkeypatch, context_name):
    from agent import delegation_context

    _worker(monkeypatch, placement_host="ci-box")
    assert _build().placement_host == "ci-box"
    with getattr(delegation_context, context_name)():
        assert _build().placement_host is None
    assert _build().placement_host == "ci-box"


def test_explicit_none_kwarg_suppresses_own_host(no_worker_env, monkeypatch):
    _worker(monkeypatch, placement_host="ci-box")
    assert _build(placement_host=None).placement_host is None


def test_orphan_repair_does_not_stamp_its_own_host():
    src = (REPO / "plugins" / "blackbox" / "orphans.py").read_text()
    assert '"placement_host": None' in src


def test_blank_host_round_trips_as_null(db, no_worker_env):
    store.insert_turn(TurnRecord(turn_id="turn_b", placement_host="  "))
    assert _host(db, "turn_b") is None


# --------------------------------------------------------------------------- #
# backfill script
# --------------------------------------------------------------------------- #
SCRIPT = REPO / "scripts" / "backfill-turn-placement-host.py"


def _seed_backfill(tmp_path):
    home = tmp_path / "home"
    turns = home / "blackbox" / "turns.db"
    turns.parent.mkdir(parents=True)
    with patch("plugins.blackbox.store._db_path", return_value=turns):
        store._connect().close()
    conn = sqlite3.connect(str(turns))
    conn.executemany("INSERT INTO turns (turn_id, platform, ts_start, ts_end, placement_host) "
                     "VALUES (?, ?, ?, ?, ?)", [
        ("tw", "cli", 1000.0, 1010.0, None),      # inside run 7 window on ci-box
        ("tl", "cli", 2000.0, 2010.0, None),      # inside local run 8
        ("tn", "cli", 5000.0, 5010.0, None),      # no run covers it
        ("tx", "cli", 1001.0, 1002.0, "keep"),    # already stamped: untouched
        ("tg", "discord", 1000.0, 1010.0, None),  # gateway turn: never a worker
        ("ta", "cli", 3000.0, 3010.0, None),      # two runs on two hosts: ambiguous
    ])
    conn.commit()
    conn.close()
    board = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(board))
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,"
                 " started_at INTEGER, ended_at INTEGER, host TEXT)")
    conn.executemany("INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?)", [
        (7, "t_a", "daedalus", 990, 1100, "ci-box"),
        (8, "t_b", "daedalus", 1990, 2100, "studio-local"),
        (9, "t_c", "daedalus", 2990, 3100, "ci-box"),
        (10, "t_d", "daedalus", 2995, 3100, "ace-media"),
        (11, "t_e", "other", 4990, 5100, "ci-box"),   # another profile's run
    ])
    conn.commit()
    conn.close()
    with sqlite3.connect(turns) as conn:
        conn.execute("UPDATE turns SET turn_id = 'worker-7:task:tw' WHERE turn_id = 'tw'")
        conn.execute("UPDATE turns SET turn_id = 'worker-8:task:tl' WHERE turn_id = 'tl'")
    with sqlite3.connect(board) as conn:
        conn.execute("ALTER TABLE task_runs ADD COLUMN metadata TEXT")
        conn.executemany("UPDATE task_runs SET metadata = ? WHERE id = ?", [
            (json.dumps({"worker_session_id": "worker-7"}), 7),
            (json.dumps({"worker_session_id": "worker-8"}), 8),
        ])
    return home, turns, board


def test_backfill_time_overlap_is_not_worker_provenance(tmp_path):
    home, turns, board = _seed_backfill(tmp_path)
    with sqlite3.connect(turns) as conn:
        conn.execute("UPDATE turns SET turn_id = 'interactive:task:tw' "
                     "WHERE turn_id = 'worker-7:task:tw'")
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") is None


@pytest.mark.parametrize("host", [None, "", "ci-box"])
def test_backfill_counts_all_overlapping_runs(tmp_path, host):
    home, turns, board = _seed_backfill(tmp_path)
    with sqlite3.connect(board) as conn:
        conn.execute("INSERT INTO task_runs VALUES (12, 't_unknown', 'daedalus', 995, 1090, ?, NULL)",
                     (host,))
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") is None


def test_local_boot_ignores_later_placement_env(no_worker_env, monkeypatch):
    _worker(monkeypatch)
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "ci-box"}))
    assert _build().placement_host == "studio-local"


@pytest.mark.parametrize("end", [None, 0])
def test_backfill_skips_unfinished_turn(tmp_path, end):
    home, turns, board = _seed_backfill(tmp_path)
    with sqlite3.connect(turns) as conn:
        conn.execute("UPDATE turns SET ts_end = ? WHERE turn_id = 'worker-7:task:tw'", (end,))
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") is None


@pytest.mark.parametrize("target", ["board", "turns"])
def test_backfill_escapes_sqlite_uri_paths(tmp_path, target):
    home, turns, board = _seed_backfill(tmp_path)
    if target == "board":
        renamed = board.with_name("kanban#prod?.db")
        board.rename(renamed)
        board = renamed
    else:
        renamed = home.with_name("home#prod?")
        home.rename(renamed)
        home = renamed
        turns = home / "blackbox" / "turns.db"
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") == "ci-box"
    assert not (tmp_path / "kanban").exists()
    if target == "turns":
        assert not (tmp_path / "home").exists()


def test_backfill_final_turn_may_finish_after_run_closes(tmp_path):
    home, turns, board = _seed_backfill(tmp_path)
    with sqlite3.connect(turns) as conn:
        conn.execute("UPDATE turns SET ts_start = 1099.5, ts_end = 1102.5 "
                     "WHERE turn_id = 'worker-7:task:tw'")
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") == "ci-box"


@pytest.mark.parametrize("case", ["starts_after_close", "competing_run"])
def test_backfill_final_turn_still_requires_unambiguous_run(tmp_path, case):
    home, turns, board = _seed_backfill(tmp_path)
    start = 1100.5 if case == "starts_after_close" else 1099.5
    with sqlite3.connect(turns) as conn:
        conn.execute("UPDATE turns SET ts_start = ?, ts_end = 1102.5 "
                     "WHERE turn_id = 'worker-7:task:tw'", (start,))
    if case == "competing_run":
        with sqlite3.connect(board) as conn:
            conn.execute("INSERT INTO task_runs VALUES "
                         "(12, 't_next', 'daedalus', 1101, 1200, NULL, NULL)")
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") is None


@pytest.mark.parametrize("failures", [0, 1, 3])
def test_backfill_skips_runs_with_placement_reapply_failures(tmp_path, failures):
    home, turns, board = _seed_backfill(tmp_path)
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE task_runs SET metadata = ? WHERE id = 7",
                     (json.dumps({"worker_session_id": "worker-7",
                                  "placement_reapply_failed": failures}),))
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") == (None if failures else "ci-box")
    assert _host(turns, "tl") == "studio-local"


@pytest.mark.parametrize("backend", ["ssh", "docker", "modal"])
def test_unplaced_remote_backend_is_not_studio(no_worker_env, monkeypatch, backend):
    from tools import terminal_tool

    _worker(monkeypatch)
    monkeypatch.setattr(terminal_tool, "_ensure_terminal_env_bridged", lambda: None)
    monkeypatch.setenv("TERMINAL_ENV", backend)
    assert _build().placement_host is None


def test_abandoned_subagent_record_outside_child_context(db, no_worker_env, monkeypatch):
    _worker(monkeypatch, placement_host="ci-box")
    rec = blackbox._abandoned_record(
        {"turn_id": "abandoned-child", "session_id": "child", "is_subagent": True}, {},
    )
    assert rec.is_subagent
    store.insert_turn(rec)
    assert _host(db, "abandoned-child") is None
    assert _build().placement_host == "ci-box"


def test_backfill_open_run_is_overlap_only(tmp_path):
    home, turns, board = _seed_backfill(tmp_path)
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE task_runs SET ended_at = NULL WHERE id = 7")
    module = runpy.run_path(str(SCRIPT))
    assert ("worker-7:task:tw", "ci-box") not in module["_plan"](turns, board, "daedalus")
    # Even an unfinished run with no host/provenance must block another run.
    with sqlite3.connect(board) as conn:
        conn.execute("INSERT INTO task_runs VALUES "
                     "(12, 't_next', 'daedalus', 1995, NULL, NULL, NULL)")
    assert module["_plan"](turns, board, "daedalus") == []


@pytest.mark.parametrize("changed", ["turn", "provenance"])
def test_backfill_revalidates_turn_after_backup(tmp_path, monkeypatch, changed):
    home, turns, board = _seed_backfill(tmp_path)
    module = runpy.run_path(str(SCRIPT))
    real_connect = sqlite3.connect

    class BackupConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if sql.startswith("VACUUM INTO"):
                # A provisional row changes after planning, before the update.
                if changed == "turn":
                    with real_connect(turns) as other:
                        other.execute("UPDATE turns SET ts_end = 5001 WHERE turn_id = ?",
                                      ("worker-7:task:tw",))
                else:
                    with real_connect(board) as other:
                        other.execute("UPDATE task_runs SET metadata = ? WHERE id = 7",
                                      (json.dumps({"worker_session_id": "worker-7",
                                                   "placement_reapply_failed": 1}),))
            return result

    def connect(database, *args, **kwargs):
        if str(database) == str(turns):
            kwargs["factory"] = BackupConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--home", str(home), "--board", str(board),
                                     "--profile", "daedalus", "--apply"])
    assert module["main"]() == 0
    assert _host(turns, "tw") is None
    assert _host(turns, "tl") == "studio-local"


def test_backfill_open_run_failure_between_reads(tmp_path, monkeypatch):
    home, turns, board = _seed_backfill(tmp_path)
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE task_runs SET ended_at = NULL WHERE id = 7")
    module = runpy.run_path(str(SCRIPT))
    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        if str(database).startswith(turns.resolve().as_uri()):
            with real_connect(board) as other:
                other.execute("UPDATE task_runs SET metadata = ? WHERE id = 7",
                              (json.dumps({"worker_session_id": "worker-7",
                                           "placement_reapply_failed": 1}),))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert ("worker-7:task:tw", "ci-box") not in module["_plan"](turns, board, "daedalus")


def test_backfill_studio_dispatch_is_not_execution_provenance(tmp_path):
    home, turns, board = _seed_backfill(tmp_path)
    with sqlite3.connect(board) as conn:
        conn.execute("UPDATE task_runs SET host = 'studio' WHERE id = 8")
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tl") is None


def test_backfill_board_locked_through_apply(tmp_path, monkeypatch):
    home, turns, board = _seed_backfill(tmp_path)
    module = runpy.run_path(str(SCRIPT))
    real_connect = sqlite3.connect
    blocked = []

    class ApplyConnection(sqlite3.Connection):
        def executemany(self, sql, parameters):
            if sql.startswith("UPDATE turns SET placement_host"):
                with real_connect(board, timeout=0) as other:
                    try:
                        other.execute("UPDATE task_runs SET metadata = '{}' WHERE id = 7")
                    except sqlite3.OperationalError as exc:
                        assert "locked" in str(exc)
                        blocked.append(True)
            return super().executemany(sql, parameters)

    def connect(database, *args, **kwargs):
        if str(database) == str(turns):
            kwargs["factory"] = ApplyConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--home", str(home), "--board", str(board),
                                     "--profile", "daedalus", "--apply"])
    assert module["main"]() == 0
    assert blocked == [True]
    assert _host(turns, "tw") == "ci-box"


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=60)


def test_backfill_dry_run_is_default_and_writes_nothing(tmp_path):
    home, turns, board = _seed_backfill(tmp_path)
    before = turns.read_bytes()
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus")
    assert out.returncode == 0, out.stderr
    assert "mode=dry-run" in out.stdout
    assert "would_stamp=2" in out.stdout
    assert turns.read_bytes() == before
    assert _host(turns, "tw") is None


def test_backfill_apply_stamps_only_covered_null_rows(tmp_path):
    home, turns, board = _seed_backfill(tmp_path)
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") == "ci-box"
    assert _host(turns, "tl") == "studio-local"
    assert _host(turns, "tn") is None
    assert _host(turns, "tx") == "keep"
    assert _host(turns, "tg") is None
    assert _host(turns, "ta") is None


class _Counted(float):
    """A timestamp that counts every ordering comparison made on it."""
    ops = 0

    def _counting(name):  # noqa: N805 - class-body helper
        base = getattr(float, name)

        def method(self, other):
            _Counted.ops += 1
            return base(self, other)
        return method

    __lt__ = _counting("__lt__")
    __le__ = _counting("__le__")
    __gt__ = _counting("__gt__")
    __ge__ = _counting("__ge__")
    del _counting


class _Rows(list):
    def fetchall(self):
        return list(self)


class _FakeConn:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows

    def execute(self, sql, *params):
        if sql.startswith("PRAGMA"):
            return _Rows((i, c) for i, c in enumerate(self.cols))
        return _Rows(self.rows)


def test_backfill_run_lookup_is_not_quadratic():
    # Prism 525c863d6abc: comparisons, not wall time. One closed run and one
    # turn per window; a per-turn scan of every run costs ~R*T comparisons.
    n = 1000
    runs = [(_Counted(i * 100), _Counted(i * 100 + 50), "ci-box",
             json.dumps({"worker_session_id": f"w{i}"})) for i in range(n)]
    turns = [(f"w{i}:task:n", _Counted(i * 100 + 10), _Counted(i * 100 + 20), "cli")
             for i in range(n)]
    module = runpy.run_path(str(SCRIPT))
    _Counted.ops = 0
    plan = module["_plan"](Path("turns.db"), Path("kanban.db"), "daedalus",
                           turns_conn=_FakeConn(["turn_id", "placement_host"], turns),
                           board_conn=_FakeConn(["metadata"], runs))
    assert len(plan) == n
    # O((R+T) log R) is ~3e4 here; the old per-turn scan made ~1.5e6.
    assert _Counted.ops < 10 * 2 * n * n.bit_length(), _Counted.ops


def test_backfill_run_index_matches_full_scan():
    # The index must keep the full scan's verdict: exactly one overlapping run,
    # with open runs (ended_at NULL) and boundary ties counted as overlaps.
    import random

    module = runpy.run_path(str(SCRIPT))
    rng = random.Random(0xC9FC)
    for _ in range(300):
        runs = []
        for i in range(rng.randrange(0, 8)):
            start = rng.randrange(0, 60)
            end = None if rng.random() < 0.2 else start + rng.randrange(0, 20)
            runs.append((start, end, f"h{i}", None))
        index = module["_run_index"](runs)
        for _ in range(20):
            ts_start = rng.randrange(0, 80)
            ts_end = ts_start + rng.randrange(0, 10)
            scan = [r for r in runs if r[0] <= ts_end and (r[1] is None or ts_start <= r[1])]
            assert module["_only_overlap"](index, ts_start, ts_end) == (
                scan[0] if len(scan) == 1 else None), (runs, ts_start, ts_end)
