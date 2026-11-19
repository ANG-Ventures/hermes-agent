"""A cohort death names who sent the signal (t_c6d2e782).

On 2026-11-17 the Studio dispatcher logged "kanban cohort death: 12 workers ended
externally in one tick" (11:06:42) and "17 workers" (20:57:11) and nothing else.
The exec flight recorder had the sender of the first: a worker's
``sudo -n pkill -f '^/usr/bin/eslogger exec$' -P 60886`` at 11:06:16. The second
killed the recorder's own eslogger before it flushed, so the ring has nothing.

Pinned here, on a synthetic ring in the recorder's format:
  * a pkill in the window is named with its parent chain (the session's command);
  * a targeted ``kill <pid>`` of a non-member is another session's business;
    the same kill of a member pid is the sender;
  * ``kill -0`` probes and execs outside the window are ignored;
  * no ring / nothing found -> None, and the page then says SENDER UNKNOWN;
  * ``detect_crashed_workers`` hands the members' pids to the lookup and puts the
    sender in the log line and the page.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

# 2026-11-17 11:06:42 PST, the first detection. Rows are built inside each test: the
# suite may pin TZ after import, and ``ts`` is local time like the recorder's.
NOW = 1794942402.0


def _ts(offset: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW + offset))


def _row(offset, pid, ppid, uid, exe, args):
    return json.dumps({"ts": _ts(offset), "pid": pid, "ppid": ppid, "uid": uid, "exe": exe, "args": args,
                       "argv_fp": "x", "cwd": "/"}, separators=(",", ":"))


@pytest.fixture
def ring(tmp_path, monkeypatch):
    d = tmp_path / "exec-flight"
    d.mkdir()
    monkeypatch.setenv("HERMES_EXEC_FLIGHT_DIRS", str(d))

    def write(*rows):
        path = d / (time.strftime("%Y-%m-%d-%H", time.localtime(NOW)) + ".jsonl")
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    return write


# The 11:06:16 chain, as the recorder wrote it (pids from the incident).
def _incident():
    return [
        _row(-170, 61140, 61130, 502, "/bin/zsh",
             "zsh -c cd /Volumes/fleet-scratch/tmp/t_1d5ff6b8; rm -f es.jsonl; sudo -n /usr/bin/eslogger exec"),
        _row(-26, 87938, 61140, 0, "/usr/bin/sudo", "sudo -n pkill -f ^/usr/bin/eslogger exec$ -P 60886"),
        _row(-26, 88024, 87938, 0, "/usr/bin/pkill", "pkill -f ^/usr/bin/eslogger exec$ -P 60886"),
    ]


def test_names_the_pkill_and_its_session(ring):
    ring(*_incident())
    sender = kb._cohort_death_sender(now=NOW)
    assert sender is not None
    assert sender.startswith(f"`pkill -f ^/usr/bin/eslogger exec$ -P 60886` (pid 88024, uid 0, {_ts(-26)[11:]})")
    assert "`sudo -n pkill" in sender and "t_1d5ff6b8" in sender, sender


def test_targeted_kill_counts_only_for_a_member(ring):
    ring(
        _row(-60, 32523, 32375, 0, "/usr/bin/sudo", "sudo -n kill 17734"),
        _row(-60, 32543, 32523, 0, "/bin/kill", "kill 17734"),
    )
    assert kb._cohort_death_sender(now=NOW, pids={91837, 91838}) is None
    assert "`kill 17734`" in kb._cohort_death_sender(now=NOW, pids={17734})


def test_probes_and_old_execs_are_ignored(ring):
    ring(
        _row(-10, 500, 1, 502, "/bin/kill", "kill -0 91837"),
        _row(-600, 501, 1, 0, "/usr/bin/pkill", "pkill -f worker"),
        _row(+60, 502, 1, 0, "/usr/bin/pkill", "pkill -f later"),
    )
    assert kb._cohort_death_sender(now=NOW, pids={91837}) is None


def test_no_ring_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_EXEC_FLIGHT_DIRS", str(tmp_path / "absent"))
    assert kb._cohort_death_sender(now=NOW) is None


def _page_body(monkeypatch, conn, **kw):
    from hermes_cli import kanban as kanban_cli

    calls = []
    monkeypatch.setattr(kanban_cli, "_notify_script_path", lambda: "/nonexistent/notify.py")
    monkeypatch.setattr(subprocess, "run", lambda argv, **_k: calls.append(argv))
    kb._page_cohort_death(conn, ["t_a", "t_b", "t_c"], **kw)
    [argv] = calls
    return argv[argv.index("--send") + 1]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_EXEC_FLIGHT_DIRS", str(tmp_path / "no-exec-flight"))
    kb.init_db()
    kb._recent_worker_exits.clear()
    with kb.connect_closing() as conn:
        yield conn
    kb._recent_worker_exits.clear()


def test_page_names_the_sender_or_says_unknown(board, monkeypatch):
    named = _page_body(monkeypatch, board, still_alive=[], sender="`pkill -f x -P 1` (pid 9, uid 0, 11:06:16)")
    assert "Signal sender (exec flight recorder): `pkill -f x -P 1`" in named
    unknown = _page_body(monkeypatch, board, still_alive=[], sender=None)
    assert "SENDER UNKNOWN" in unknown


@pytest.mark.platforms("linux", "macos")
def test_detect_crashed_workers_logs_and_pages_the_sender(board, monkeypatch, caplog):
    procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True) for _ in range(3)]
    try:
        now = int(time.time())
        for i, proc in enumerate(procs):
            tid = kb.create_task(board, title=f"sender probe {i}", assignee="worker")
            task = kb.claim_task(board, tid)
            assert kbd._set_worker_pid(board, tid, proc.pid, run_id=task.current_run_id)
            board.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?", (now - 5, tid))
        for proc in procs:
            os.kill(proc.pid, signal.SIGTERM)
        for proc in procs:
            proc.wait(timeout=10)

        seen_pids = []
        pages = []
        monkeypatch.setattr(kb, "_cohort_death_sender",
                            lambda pids=(), **_k: seen_pids.append(set(pids)) or "`pkill -f -P` (pid 7)")
        monkeypatch.setattr(kb, "_page_cohort_death",
                            lambda conn, ids, still_alive=None, sender=None: pages.append(sender))
        with caplog.at_level(logging.ERROR):
            kbd.detect_crashed_workers(board)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    assert pages == ["`pkill -f -P` (pid 7)"]
    assert {p.pid for p in procs} <= seen_pids[0]
    assert any("sender: `pkill -f -P` (pid 7)" in r.getMessage() for r in caplog.records)
