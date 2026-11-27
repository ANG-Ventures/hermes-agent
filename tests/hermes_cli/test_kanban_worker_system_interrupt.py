"""A worker that exited 130 because its OWN process stopped the turn is not a task failure.

2026-11-10: nine workers exited rc=130 when their session-turn-lease refresher
interrupted the turn (``interrupted_by_system(session_turn_lease_lost)``). The sweep
booked each as ``nonzero_exit`` -> ``crashed`` -> ``consecutive_failures++``, and
t_526d18f1 was ``gave_up`` for a fault that was not its own (t_69b98ec5). Since
ANG-Ventures/hermes-agent#227 the worker log names the stop on the line before the
exit trailer; the sweep reads it and requeues without a strike. A user/operator stop
(``interrupted_by_user``), a bare 130 (SIGINT) and a signal-driven exit still count.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

_LEASE_LOST = "interrupted_by_system(session_turn_lease_lost)"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _dead_worker_with_log(conn, tid: str, pid: int, rc: int, interrupted: str | None = None) -> None:
    """Claim ``tid`` for a worker that already exited ``rc`` and wrote its log, as
    ``cli_single_query._raise_worker_exit`` writes it — never reaped here."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.commit()
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    stop = f"\n[kanban-worker-exit] interrupted: {interrupted}\n" if interrupted else ""
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"the model said something\n{stop}\n{KANBAN_WORKER_EXIT_TRAILER}{rc}\n")


def _last(conn, tid):
    ev = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    run = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,)).fetchone()
    return ev, run


@pytest.mark.parametrize("reason", [_LEASE_LOST, "interrupted_during_api_call(session_turn_lease_lost)"])
def test_system_interrupt_requeues_without_a_strike(kanban_home, reason):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _dead_worker_with_log(conn, tid, 72001, 130, interrupted=reason)

        assert kbd.detect_crashed_workers(conn) == []

        ev, run = _last(conn, tid)
        task = kb.get_task(conn, tid)
        meta = kb._json_dict(run["metadata"])
        assert ev["kind"] == "system_interrupted"
        assert run["outcome"] == "system_interrupted"
        assert meta["exit_code"] == 130
        assert meta["turn_exit_reason"] == reason
        assert meta["interrupt_issuer"] == "session_turn_lease_lost"
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_system_interrupt_does_not_trip_a_limit_of_one(kanban_home):
    """The t_526d18f1 shape: effective limit 1, a lease-lost exit must not give the card up."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", max_retries=1)
        _dead_worker_with_log(conn, tid, 72101, 130, interrupted=_LEASE_LOST)
        kbd.detect_crashed_workers(conn)
        kb.recompute_ready(conn, failure_limit=1)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()[0] == 0


@pytest.mark.parametrize("interrupted", [None, "interrupted_by_user", "interrupted_during_api_call"])
def test_user_stop_or_bare_130_still_counts(kanban_home, interrupted):
    """No system issuer = a human/operator stop or SIGINT: a crash that ticks the counter."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _dead_worker_with_log(conn, tid, 72201, 130, interrupted=interrupted)

        kbd.detect_crashed_workers(conn)

        ev, run = _last(conn, tid)
        assert run["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_system_line_with_another_exit_code_still_counts(kanban_home):
    """The interrupted line only excuses the rc=130 it precedes."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _dead_worker_with_log(conn, tid, 72301, 1, interrupted=_LEASE_LOST)
        kbd.detect_crashed_workers(conn)
        _, run = _last(conn, tid)
        assert run["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_stale_interrupt_line_from_an_earlier_exit_does_not_excuse_a_later_one(kanban_home):
    """Only the line directly before the LAST trailer counts: an older run's system stop
    in the same append-mode log does not make a later bare 130 free."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        log = kb.worker_log_path(tid)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            f"\n[kanban-worker-exit] interrupted: {_LEASE_LOST}\n\n{KANBAN_WORKER_EXIT_TRAILER}130\n",
            encoding="utf-8",
        )
        _dead_worker_with_log(conn, tid, 72401, 130, interrupted=None)
        kbd.detect_crashed_workers(conn)
        _, run = _last(conn, tid)
        assert run["outcome"] == "crashed"


def test_repeated_system_interrupts_are_bounded(kanban_home):
    """A card whose every run is stopped by a system watchdog must not respawn forever:
    past the free streak the next one is booked as a crash and counts."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        limit = kbd._SYSTEM_INTERRUPT_FREE_LIMIT
        for i in range(limit):
            _dead_worker_with_log(conn, tid, 72500 + i, 130, interrupted=_LEASE_LOST)
            kbd.detect_crashed_workers(conn)
            assert _last(conn, tid)[1]["outcome"] == "system_interrupted"
        assert kb.get_task(conn, tid).consecutive_failures == 0
        _dead_worker_with_log(conn, tid, 72599, 130, interrupted=_LEASE_LOST)
        kbd.detect_crashed_workers(conn)
        _, run = _last(conn, tid)
        assert run["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1
