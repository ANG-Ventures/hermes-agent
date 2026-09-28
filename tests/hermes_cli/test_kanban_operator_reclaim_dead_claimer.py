"""t_6451e7c9: ``reclaim --operator`` releases a dead-claimer launch-bound hold.

(The gateway-spawned CLI session identity half of the card landed in
ANG-Ventures/hermes-agent#1403.)

``claim --review`` from that CLI stamps ``host:<cli pid>`` as the claim
lock. The CLI exits at once, so the claimer is dead with no worker pid, and the
dead-claimer launch bound held the card a full claim TTL. ``--operator`` could
not release it (live incident t_a578a202 run 13789).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import psutil
import secrets
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _dead_pid() -> int:
    c = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
    c.wait(timeout=10)
    assert not psutil.pid_exists(c.pid)
    return c.pid


def _running_card(conn, lock, *, worker_pid=None):
    tid = kb.create_task(conn, title="review claim", assignee="daedalus")
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
        "worker_pid=?, started_at=? WHERE id=?",
        (lock, now + 900, worker_pid, now, tid),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
        "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
        (tid, lock, now + 900, worker_pid, now),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, tid))
    conn.commit()
    return tid, run_id


def _host(pid) -> str:
    return f"{kb._claimer_id().split(':', 1)[0]}:{pid}"


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else {}
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (tid, kind),
        )
    ]


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


@pytest.fixture
def operator_token(kanban_home, monkeypatch):
    """Present the operator token the way a real operator does."""
    path = kb.operator_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(16)
    path.write_text(value, encoding="utf-8")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, value)
    return value


class _FakeProc:
    def __init__(self, pid, cmdline, status="sleeping", euid=None):
        self.info = {
            "pid": pid, "cmdline": cmdline, "status": status,
            "uids": None if euid is None else SimpleNamespace(real=euid, effective=euid),
        }


# --- operator release of a dead-claimer hold ---------------------------------


def test_operator_releases_dead_claimer_inside_launch_bound(conn, operator_token):
    lock = _host(_dead_pid())
    tid, run_id = _running_card(conn, lock)
    # RED before the fix: operator had no effect inside the launch bound.
    assert kb.reclaim_task(conn, tid, reason="no op") is False
    assert kb.reclaim_task(
        conn, tid, reason="stale review claim", operator="apollo: cli claimer gone",
    ) is True
    row = conn.execute("SELECT status, claim_lock FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["claim_lock"] is None and row["status"] != "running"
    ev = _events(conn, tid, "reclaimed")[-1]
    assert ev["operator_override"] == "apollo: cli claimer gone"
    assert ev["operator_override_basis"] == "dead_claimer_no_worker_pid"
    assert conn.execute(
        "SELECT outcome FROM task_runs WHERE id=?", (run_id,),
    ).fetchone()[0] == "reclaimed"


def test_operator_cannot_release_live_claimer(conn, operator_token):
    tid, _ = _running_card(conn, _host(os.getpid()))
    assert kb.reclaim_task(conn, tid, operator="apollo: x") is False
    assert _events(conn, tid, "reclaim_refused")[-1]["reason"] == "liveness_unprovable"
    assert not _events(conn, tid, "reclaimed")


def test_operator_cannot_release_dead_claimer_with_worker_heartbeat(conn, operator_token):
    lock = _host(_dead_pid())
    tid, _ = _running_card(conn, lock)
    assert kb.heartbeat_worker(conn, tid, note="unstamped orphan alive")
    assert kb.reclaim_task(conn, tid, operator="apollo: x") is False
    assert not _events(conn, tid, "reclaimed")


def test_operator_cannot_release_when_orphan_process_names_task(conn, operator_token):
    lock = _host(_dead_pid())
    tid, _ = _running_card(conn, lock)
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; time.sleep(60)", tid],
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        time.sleep(0.2)
        assert kb.reclaim_task(conn, tid, operator="apollo: x") is False
        assert not _events(conn, tid, "reclaimed")
    finally:
        orphan.kill()
        orphan.wait(timeout=10)


def test_operator_override_without_token_is_refused(conn, monkeypatch):
    """FleetReview #1404: a bare --operator string is not authority."""
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    lock = _host(_dead_pid())
    tid, _ = _running_card(conn, lock)
    assert kb.reclaim_task(conn, tid, operator="anyone: arbitrary") is False
    assert not _events(conn, tid, "reclaimed")
    ev = _events(conn, tid, "reclaim_refused")[-1]
    assert ev["operator_override_refused"] == "token_absent"
    assert "operator_override" not in ev


def test_operator_override_with_wrong_token_is_refused(conn, operator_token, monkeypatch):
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "not-" + operator_token)
    tid, _ = _running_card(conn, _host(_dead_pid()))
    assert kb.reclaim_task(conn, tid, operator="apollo: x") is False
    assert _events(conn, tid, "reclaim_refused")[-1]["operator_override_refused"] == "token_mismatch"


def test_cli_gate_covers_reclaim_operator_only(conn, monkeypatch):
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    tid, _ = _running_card(conn, _host(_dead_pid()))
    with pytest.raises(kb.OperatorTokenRequiredError):
        kb.enforce_operator_flag_gate(conn, [tid], "reclaim", flags=["--operator"])
    assert _events(conn, tid, "takeover_refused")[-1]["flags"] == ["--operator"]
    # --takeover on reclaim keeps its prior, ungated behaviour.
    kb.enforce_operator_flag_gate(conn, [tid], "reclaim", flags=["--takeover"])
    assert len(_events(conn, tid, "takeover_refused")) == 1


_POSIX_UIDS = pytest.mark.skipif(
    not hasattr(os, "geteuid"), reason="uid-based skip is POSIX-only",
)


def _scan_with(monkeypatch, procs):
    monkeypatch.setattr(kb.psutil, "process_iter", lambda attrs=None: iter(procs))
    return kb._host_process_mentions_task("t_scan")


@_POSIX_UIDS
def test_unreadable_same_user_process_is_inconclusive(monkeypatch):
    """FleetReview #1404: unreadable argv is not proof no worker exists."""
    me = getattr(os, "geteuid")()
    assert _scan_with(monkeypatch, [_FakeProc(999991, None, euid=me)]) is True
    # uid unreadable too: still fail closed.
    assert _scan_with(monkeypatch, [_FakeProc(999992, None)]) is True


@_POSIX_UIDS
def test_unreadable_zombie_or_other_uid_is_not_a_worker(monkeypatch):
    me = getattr(os, "geteuid")()
    assert _scan_with(monkeypatch, [
        _FakeProc(999993, None, status=psutil.STATUS_ZOMBIE, euid=me),
        _FakeProc(999994, None, euid=me + 1),
        _FakeProc(999995, ["python", "other-task"], euid=me),
    ]) is False
    assert _scan_with(monkeypatch, [_FakeProc(999996, ["w", "t_scan"], euid=me)]) is True


def test_cli_reclaim_passes_operator(monkeypatch):
    seen = {}

    def fake(conn, task_id, **kw):
        seen.update(kw)
        return False

    monkeypatch.setattr(kb, "reclaim_task", fake)
    import argparse
    import contextlib
    monkeypatch.setattr(kb, "connect_closing", lambda *a, **k: contextlib.nullcontext(None))
    kc._cmd_reclaim(argparse.Namespace(task_id="t_x", reason="r", operator="apollo: why"))
    assert seen == {"reason": "r", "operator": "apollo: why"}
