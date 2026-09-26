"""A worker that dies on a provider wall before the model loop is a guarded requeue.

Regression for t_a7202bf6 (2026-09-25): Codex-lane workers exited 1 with
"Codex credential is in cooldown." in their log. The dispatcher only honoured
the EX_TEMPFAIL sentinel, so each death was a ``crashed`` + ``gave_up`` pair
instead of a silent ``respawn_guarded reason=rate_limit_cooldown``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _die_with_exit_1(conn, tid, output, monkeypatch, fake_pid=86099):
    from hermes_cli.kanban_worker_exit import exit_file

    host = kb._claimer_id().split(":", 1)[0]
    assert kb.claim_task(conn, tid, claimer=f"{host}:w") is not None
    task = kb.get_task(conn, tid)
    assert task is not None and task.current_run_id is not None
    kb._set_worker_pid(conn, tid, fake_pid)
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{tid}.log"
    kb._stamp_worker_log_run_boundary(log_path)
    with open(log_path, "ab") as fh:
        fh.write(output.encode("utf-8") + b"\n")
    db_path = Path(next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"))
    receipt = exit_file(db_path, tid, task.current_run_id)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"exit_code": 1, "exit_class": None}), encoding="utf-8")
    monkeypatch.setattr(kb, "_pid_alive", lambda _p: False)
    return kb.detect_crashed_workers(conn)


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, tid)]


def test_credential_cooldown_exit_is_guarded_not_crashed(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="codex lane", assignee="daedalus-sol", max_retries=1)
        crashed = _die_with_exit_1(
            conn, tid,
            "Warning: Unknown toolsets: rl\nCodex credential is in cooldown.",
            monkeypatch,
        )
        assert crashed == []
        kinds = _kinds(conn, tid)
        assert "crashed" not in kinds and "gave_up" not in kinds
        assert "rate_limited" in kinds
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
        run = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()
        assert run["outcome"] == "rate_limited"


def test_genuine_nonzero_exit_still_crashes(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="real bug", assignee="worker", max_retries=1)
        crashed = _die_with_exit_1(
            conn, tid, "Traceback (most recent call last):\nKeyError: 'model'", monkeypatch,
        )
        assert crashed == [tid]
        kinds = _kinds(conn, tid)
        assert "crashed" in kinds and "rate_limited" not in kinds


def test_cooldown_text_only_counts_in_the_runs_last_lines(kanban_home, monkeypatch):
    """An early mention followed by a real traceback is a crash, not a wall."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="mixed", assignee="worker", max_retries=1)
        crashed = _die_with_exit_1(
            conn, tid,
            "Codex credential is in cooldown.\nfalling back to claude\nstep 1\nstep 2\n"
            "Traceback (most recent call last):\nKeyError: 'model'",
            monkeypatch,
        )
        assert crashed == [tid]


def test_mutant_classification_off_takes_crashed_path(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_stderr_cooldown_class", lambda _seg: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="codex lane", assignee="daedalus-sol", max_retries=1)
        crashed = _die_with_exit_1(conn, tid, "Codex credential is in cooldown.", monkeypatch)
        assert crashed == [tid]
        assert "crashed" in _kinds(conn, tid)
