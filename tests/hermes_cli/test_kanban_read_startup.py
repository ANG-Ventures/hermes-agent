"""Read CLI paths do not initialize SQLite, spawn git, or load dispatch machinery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

ROOT = Path(__file__).resolve().parents[2]


def seed(path):
    with kbc.connect_closing(db_path=path) as conn:
        task = kb.create_task(conn, title="startup fixture")
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))
    return task


def test_read_connection_never_initializes_and_is_sqlite_readonly(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    task_id = seed(db)
    def forbidden(*args, **kwargs):
        pytest.fail("read connection entered initialization")
    monkeypatch.setattr(kbc, "_cross_process_init_lock", forbidden)
    with kbc.connect_closing(db_path=db, readonly=True) as conn:
        assert kb.get_task(conn, task_id).title == "startup fixture"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        conn.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE tasks SET title='no'")
    with pytest.raises(sqlite3.OperationalError):
        kbc.connect(db_path=tmp_path / "missing.db", readonly=True)
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("action", ["show", "placement", "context", "runs", "attachments"])
def test_cli_reads_under_held_writer_and_init_lock(tmp_path, action):
    if sys.platform == "win32":
        pytest.skip("POSIX init-lock contention")
    import fcntl
    db = tmp_path / "kanban.db"
    task_id = seed(db)
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_KANBAN_DB": str(db),
           "HERMES_KANBAN_BUSY_TIMEOUT_MS": "100", "PYTHONPATH": str(ROOT)}
    argv = [action] + ([] if action == "context" else ["--json"])
    if action == "list":
        argv += ["--board", "default"]
    if action != "list":
        argv.insert(1, task_id)
    writer = sqlite3.connect(db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with db.with_suffix(".db.init.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [sys.executable, "-m", "hermes_cli.main", "kanban", *argv],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
                stdin=subprocess.DEVNULL,
            )
        assert result.returncode == 0, result.stderr
        assert "database is locked" not in result.stderr
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("output_flags", [[], ["--json"]])
def test_show_imports_no_dispatch_pool_gateway_or_git(tmp_path, output_flags):
    db = tmp_path / "kanban.db"
    task_id = seed(db)
    code = '''
import json, runpy, subprocess, sys
real = subprocess.Popen
launches = []
def counted(argv, *a, **kw):
    launches.append(str(argv))
    return real(argv, *a, **kw)
subprocess.Popen = counted
sys.argv = ['hermes', 'kanban', 'show', sys.argv[1], *sys.argv[2:]]
runpy.run_module('hermes_cli.main', run_name='__main__')
for name in sys.modules:
    assert name not in ('hermes_cli.kanban_db_dispatch', 'hermes_cli.kanban_worker_pool'), name
    assert not name.startswith('gateway'), name
assert not launches, launches
'''
    result = subprocess.run(
        [sys.executable, "-c", code, task_id, *output_flags], cwd=ROOT,
        env={**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_KANBAN_DB": str(db),
             "PYTHONPATH": str(ROOT)}, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    if output_flags:
        assert json.loads(result.stdout)["task"]["id"] == task_id
    else:
        assert task_id in result.stdout


def test_write_init_retries_held_writer(tmp_path, monkeypatch):
    import threading
    db = tmp_path / "board.db"
    seed(db)
    with sqlite3.connect(db) as stale:
        stale.execute("DROP TABLE kanban_initialization")
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "10")
    writer = sqlite3.connect(db, check_same_thread=False)
    writer.execute("BEGIN IMMEDIATE")
    sleeps = []
    real_sleep = kbc.time.sleep
    def release_on_backoff(delay):
        sleeps.append(delay)
        writer.rollback()
        real_sleep(delay)
    monkeypatch.setattr(kbc.time, "sleep", release_on_backoff)
    try:
        with kbc.connect_closing(db_path=db) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10
        assert sleeps, "held writer never exercised retry"
    finally:
        writer.rollback()
        writer.close()


def test_current_receipt_with_missing_column_reinitializes(tmp_path):
    # t_7a79b915: a board whose receipt reads current but lacks a declared column re-runs the migration.
    from hermes_cli import kanban_artifact_pointer as kap
    db = tmp_path / "board.db"
    task_id = seed(db)
    with sqlite3.connect(db) as stale:
        stale.execute("ALTER TABLE tasks DROP COLUMN artifact_pointer")
        kbc._record_initialization(stale)
        assert kbc._INITIALIZATION_SIGNATURE == stale.execute(
            "SELECT signature FROM kanban_initialization").fetchone()[0]
    with kbc.connect_closing(db_path=db) as conn:
        assert "artifact_pointer" in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert kbc._initialization_is_current(conn)
        with conn:
            assert kap.stamp(conn, task_id) == []


def test_provider_discovery_does_not_probe_version_until_oauth_request():
    code = '''
import subprocess
import hermes_cli.version_info as version
calls = []
def forbidden(*args, **kwargs):
    raise AssertionError("provider discovery probed git/version")
version.get_version_info = forbidden
subprocess.Popen = forbidden
from tools.environments.local_env_policy import _PROVIDER_ENV_BLOCKLIST
from hermes_cli import auth_codex, auth_constants
assert _PROVIDER_ENV_BLOCKLIST
version_info = type('Version', (), {'base_version': 'request-sentinel'})()
auth_constants.get_version_info = lambda: version_info
assert auth_codex.codex_oauth_user_agent() == 'hermes-cli/request-sentinel'
assert auth_constants.CODEX_OAUTH_USER_AGENT == 'hermes-cli/request-sentinel'
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_migration_backfill_is_atomic_across_busy_retry(tmp_path, monkeypatch):
    db = tmp_path / "migration.db"
    real = kbc._migrate_add_optional_columns
    attempts = []
    def interrupted(conn):
        attempts.append(1)
        conn.execute("CREATE TABLE IF NOT EXISTS retry_canary (id INTEGER PRIMARY KEY)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(retry_canary)")}
        if "delivery_mode" not in columns:
            conn.execute("ALTER TABLE retry_canary ADD COLUMN delivery_mode TEXT DEFAULT 'notify'")
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            conn.execute("INSERT INTO retry_canary VALUES (1, 'notify+wake')")
        real(conn)
    monkeypatch.setattr(kbc, "_migrate_add_optional_columns", interrupted)
    with kbc.connect_closing(db_path=db) as conn:
        assert conn.execute("SELECT delivery_mode FROM retry_canary").fetchone()[0] == "notify+wake"
    assert len(attempts) == 2


def test_read_scope_resets_after_error():
    with pytest.raises(ValueError):
        with kbc.read_scope():
            assert kbc._READ_ONLY.get()
            raise ValueError("fixture")
    assert not kbc._READ_ONLY.get()


def test_comment_waits_for_thirty_second_writer(tmp_path):
    import threading
    db = tmp_path / "kanban.db"
    task_id = seed(db)
    writer = sqlite3.connect(db, check_same_thread=False)
    writer.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(30, writer.rollback)
    timer.start()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "kanban", "comment", task_id,
             "writer wait canary"], cwd=ROOT,
            env={**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_KANBAN_DB": str(db),
                 "HERMES_KANBAN_BUSY_TIMEOUT_MS": "40000", "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=55,
        )
        assert result.returncode == 0, result.stderr
        with kbc.connect_closing(db_path=db, readonly=True) as conn:
            assert conn.execute("SELECT body FROM task_comments WHERE task_id=?", (task_id,)).fetchone()[0] == "writer wait canary"
    finally:
        timer.cancel()
        timer.join()
        writer.rollback()
        writer.close()


def test_current_schema_connect_does_not_need_writer(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    seed(db)
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "10")
    writer = sqlite3.connect(db)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with kbc.connect_closing(db_path=db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        writer.rollback()
        writer.close()


def test_list_recomputes_dependency_readiness(tmp_path):
    db = tmp_path / "kanban.db"
    child = seed(db)
    with kbc.connect_closing(db_path=db) as conn:
        parent = kb.create_task(conn, title="completed dependency")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.execute("INSERT INTO task_links(parent_id, child_id, kind) VALUES (?, ?, 'blocks')", (parent, child))
        conn.execute("UPDATE tasks SET status='todo', assignee='daedalus' WHERE id=?", (child,))
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "list", "--board", "default",
         "--status", "ready", "--json"], cwd=ROOT,
        env={**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_KANBAN_DB": str(db),
             "PYTHONPATH": str(ROOT)}, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert any(task["id"] == child for task in json.loads(result.stdout))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0] == "ready"


def test_readonly_kill_switch(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    seed(db)
    monkeypatch.setenv("HERMES_KANBAN_READ_ONLY", "0")
    with kbc.read_scope():
        with kbc.connect_closing(db_path=db) as conn:
            conn.execute("UPDATE tasks SET title='legacy connection'")
    with kbc.connect_closing(db_path=db, readonly=True) as conn:
        assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "legacy connection"


def test_init_receipt_uses_loaded_signature_not_disk(tmp_path, monkeypatch):
    """A process loaded at signature A must not stamp (or trust) the on-disk B (t_f5019c7b)."""
    db = tmp_path / "board.db"
    seed(db)
    monkeypatch.setattr(kbc, "_INITIALIZATION_SIGNATURE", "A", raising=False)
    monkeypatch.setattr(kbc, "_initialization_signature", lambda: "B")  # files on disk now hash to B
    with sqlite3.connect(db) as conn:
        kbc._record_initialization(conn)
        assert conn.execute("SELECT signature FROM kanban_initialization").fetchone()[0] == "A"
        conn.execute("UPDATE kanban_initialization SET signature='B'")
        conn.commit()
        assert not kbc._initialization_is_current(conn)
