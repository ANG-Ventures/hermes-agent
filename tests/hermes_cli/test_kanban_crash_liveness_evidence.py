"""A ``crashed`` / ``gave_up`` event carries the liveness evidence the sweep acted on (t_fdfa6613).

10-12: five false crashes came from a 1.000 s fingerprint skew, and the ``crashed`` payload
held only pid + claimer + stderr_tail, so finding it took a source read. The ``liveness``
block must tell a recycled pid from a fingerprint skew from a plain death without one.
"""

import json
import subprocess
import sys
import time

import pytest

from gateway.kanban_notify_failures import format_failure_notice, format_liveness_line
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _running(conn, pid: int, *, max_retries=None) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker", max_retries=max_retries)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
    return tid


def _shift_fingerprint(conn, tid: str, centiseconds: int) -> None:
    rec = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    epoch, _, start = rec.rpartition("|")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ? WHERE id = ?",
                     (f"{epoch}|{int(start) + centiseconds}", tid))


def _shift_start_token(conn, tid: str, seconds: float) -> None:
    ev = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'spawned'",
                      (tid,)).fetchone()
    payload = json.loads(ev["payload"])
    payload["start_token"] = float(payload["start_token"]) + seconds
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), ev["id"]))


def _payload(conn, tid: str, kind: str) -> dict:
    return next(e.payload for e in kb.list_events(conn, tid) if e.kind == kind)


@pytest.mark.platforms("posix")
def test_recycled_pid_and_fingerprint_skew_produce_distinguishable_liveness(board, child):
    conn = board
    if kb._pid_start_token(child.pid) is None or kbd._process_fingerprint(child.pid) is None:
        pytest.skip("no start token / fingerprint on this host")

    # Recycled: the live pid matches neither the recorded fingerprint nor the spawn start token.
    recycled = _running(conn, child.pid)
    _shift_fingerprint(conn, recycled, -360000)
    _shift_start_token(conn, recycled, -3600.0)
    # Skew: the same process, its fingerprint read 3 s off (past the 2 s tolerance); the
    # clock-step-immune start token still matches.
    skewed = _running(conn, child.pid)
    _shift_fingerprint(conn, skewed, 300)

    crashed = kbd.detect_crashed_workers(conn)
    assert recycled in crashed and skewed in crashed

    r = _payload(conn, recycled, "crashed")["liveness"]
    s = _payload(conn, skewed, "crashed")["liveness"]
    for ev in (r, s):
        assert ev["pid_exists"] is True
        assert ev["decision"] == "recycled"
        assert ev["argv_names_task"] is False
        assert ev["fingerprint_live"] and ev["fingerprint_recorded"] != ev["fingerprint_live"]
    # What tells them apart: the skewed process's start token still agrees.
    assert abs(s["start_token_recorded"] - s["start_token_live"]) < 0.05
    assert abs(r["start_token_recorded"] - r["start_token_live"]) > 1000
    assert r != s
    assert format_liveness_line(r) != format_liveness_line(s)


@pytest.mark.platforms("posix")
def test_dead_worker_crash_and_gave_up_carry_not_alive_evidence(board):
    conn = board
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    tid = _running(conn, proc.pid, max_retries=1)
    proc.kill()
    proc.wait()

    assert tid in kbd.detect_crashed_workers(conn)
    for kind in ("crashed", "gave_up"):
        ev = _payload(conn, tid, kind)["liveness"]
        assert ev["pid_exists"] is False
        assert ev["decision"] == "not-alive"
        assert ev["fingerprint_recorded"] and ev["fingerprint_live"] is None
        assert ev["start_token_live"] is None


def test_notice_renders_one_liveness_line():
    liveness = {
        "pid_exists": True, "fingerprint_recorded": "e|100", "fingerprint_live": "e|200",
        "start_token_recorded": 1.0, "start_token_live": 1.0, "argv_names_task": False,
        "decision": "recycled",
    }
    msg = format_failure_notice(
        "crashed", {"liveness": liveness}, task_id="t_1", board_tag="", tag="",
        assignee="w", directive={"cards": ["t_1"], "classification": ""},
    )
    head, evidence = msg.split("\n")
    assert "worker crashed" in head
    assert evidence == (
        "liveness: recycled · pid_exists=yes · fingerprint e|100 vs e|200 · "
        "start_token 1.0 vs 1.0 · argv_names_task=no"
    )
    # An event from an older dispatcher renders as before: one line.
    plain = format_failure_notice(
        "crashed", {}, task_id="t_1", board_tag="", tag="",
        assignee="w", directive={"cards": ["t_1"], "classification": ""},
    )
    assert "\n" not in plain
