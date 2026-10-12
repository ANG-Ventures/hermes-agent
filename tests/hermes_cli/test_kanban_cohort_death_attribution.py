"""The 2026-10-12 12:11 "cohort death" was a misjudged liveness check, not a kill.

Provenance: card t_5c37ab9d. At 12:11:25 PT timed stepped the Mac Studio clock
+0.75 s. psutil re-bases macOS ``create_time()`` by the change in the whole-second
``boot_time()``, so every worker fingerprint the gateway read afterwards was
+100 cs off the one recorded at spawn. ``_pid_recycled`` compared the strings
exactly, called 40 live workers recycled, and booked 27 as a cohort death and 13
as clean-exit protocol violations (a PREVIOUS run's ``rc=0`` trailer). The page
went to the #logs digest because it carried no ``--sev``.

Pinned here:
  * a fingerprint within the 2 s drift tolerance is the same process;
  * a clock-step-shifted fingerprint keeps live workers running (no cohort);
  * an earlier run's exit trailer is not this run's exit;
  * a real ``kill -TERM -- -<pgid>`` of 3 workers is still a cohort death, and
    its events name the spawn pgid and that the pids are gone;
  * the page is sent with ``--sev error`` and says when members were still alive.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    kb._recent_worker_exits.clear()
    with kb.connect_closing() as conn:
        yield conn
    kb._recent_worker_exits.clear()


@pytest.fixture
def pages(monkeypatch):
    sent = []
    monkeypatch.setattr(
        kb, "_page_cohort_death",
        lambda conn, ids, still_alive=None: sent.append((list(ids), list(still_alive or []))),
    )
    return sent


@pytest.fixture
def sleepers():
    procs: list[subprocess.Popen] = []

    def start(n: int, *, shared_group: bool = False) -> list[subprocess.Popen]:
        for i in range(n):
            kwargs = {"start_new_session": True}
            if shared_group:
                # One group inside this session (setpgid across sessions is EPERM).
                kwargs = {"process_group": procs[0].pid if procs else 0}
            procs.append(subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **kwargs,
            ))
        return procs

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _running_cards(conn, procs):
    tasks = []
    now = int(time.time())
    for i, proc in enumerate(procs):
        tid = kb.create_task(conn, title=f"attribution probe {i}", assignee="worker")
        task = kb.claim_task(conn, tid)
        assert kbd._set_worker_pid(conn, tid, proc.pid, run_id=task.current_run_id)
        conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?", (now - 5, tid))
        tasks.append(task)
    return tasks


def _events(conn, task, kind):
    import json

    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=?",
            (task.id, task.current_run_id, kind),
        )
    ]


def test_fingerprint_tolerates_a_clock_step_but_not_another_process():
    recorded = "|179182812796"
    assert kbd._fingerprints_agree(recorded, "|179182812896"), "+1.00 s psutil re-base"
    assert kbd._fingerprints_agree(recorded, "|179182812796")
    assert not kbd._fingerprints_agree(recorded, "|179182813096"), "3 s apart: another process"
    assert not kbd._fingerprints_agree("boot-a:1|500", "boot-b:1|500"), "another boot"
    assert not kbd._fingerprints_agree(recorded, None)


def test_clock_step_keeps_live_workers_running(board, monkeypatch, pages, sleepers):
    """The 12:11:47 shape: live workers, fingerprints read +100 cs after the step."""
    tasks = _running_cards(board, sleepers(3))
    real = kbd._process_fingerprint

    def stepped(pid):
        epoch, _, start = real(pid).rpartition("|")
        return f"{epoch}|{int(start) + 100}"

    monkeypatch.setattr(kbd, "_process_fingerprint", stepped)

    crashed = kbd.detect_crashed_workers(board)

    assert crashed == []
    assert pages == [], "no cohort death for workers that are still running"
    for t in tasks:
        assert kb.get_task(board, t.id).status == "running"


def test_previous_runs_exit_trailer_is_not_this_runs_exit(board):
    log = kb.worker_logs_dir() / "t_trailer.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "\n[hermes-kanban-run-boundary aaaa]\nrun 1 output\n"
        f"{kbd.KANBAN_WORKER_EXIT_TRAILER}0\n"
        "\n[hermes-kanban-run-boundary bbbb]\nrun 2 still working\n",
        encoding="utf-8",
    )
    assert kbd._worker_log_exit_code("t_trailer") is None
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{kbd.KANBAN_WORKER_EXIT_TRAILER}3\n")
    assert kbd._worker_log_exit_code("t_trailer") == 3


@pytest.mark.platforms("linux", "macos")
def test_group_kill_is_a_cohort_death_that_names_its_group(board, pages, sleepers):
    """Red-prove: a real ``kill -TERM -- -<pgid>`` of 3 workers sharing one group."""
    procs = sleepers(3, shared_group=True)
    tasks = _running_cards(board, procs)
    pgid = os.getpgid(procs[0].pid)
    for t, proc in zip(tasks, procs):
        [spawned] = _events(board, t, "spawned")
        assert spawned["pgid"] == pgid
        assert spawned["sid"] == os.getsid(proc.pid)
        assert spawned["ppid"] == os.getpid()

    os.killpg(pgid, signal.SIGTERM)
    for proc in procs:
        proc.wait(timeout=10)

    kbd.detect_crashed_workers(board)

    assert [kb.get_task(board, t.id).status for t in tasks] == ["ready"] * 3
    assert len(pages) == 1
    ids, still_alive = pages[0]
    assert sorted(ids) == sorted(t.id for t in tasks)
    assert still_alive == [], "the pids are gone: a real kill"
    for t in tasks:
        [death] = _events(board, t, "cohort_death")
        assert death["pid_alive"] is False
        assert death["group_members"] == []


def test_page_goes_to_alerts_and_names_live_members(board, monkeypatch):
    from hermes_cli import kanban as kanban_cli

    calls = []
    monkeypatch.setattr(kanban_cli, "_notify_script_path", lambda: "/nonexistent/notify.py")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv))

    kb._page_cohort_death(board, ["t_a", "t_b", "t_c"], still_alive=["t_b"])

    [argv] = calls
    assert argv[argv.index("--sev") + 1] == "error"
    body = argv[argv.index("--send") + 1]
    assert "still had a live pid" in body and "t_b" in body
