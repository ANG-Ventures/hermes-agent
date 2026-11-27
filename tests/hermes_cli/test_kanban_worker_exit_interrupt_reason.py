"""A Kanban worker stopped in-process names the stop in its log before the exit trailer.

On 2026-11-10 nine workers' logs ended in a bare ``[kanban-worker-exit] rc=130``, which reads as
an external SIGINT; the real sender was the session-turn-lease refresher inside each worker
(t_61a89317). The turn's own ``turn_exit_reason`` goes on the line above the trailer.
"""

from __future__ import annotations

import pytest

from hermes_cli.cli_single_query import _raise_worker_exit
from hermes_cli.kanban_worker_exit import WorkerExit
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER


def _exit_stderr(monkeypatch, capsys, result, code):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    with pytest.raises(WorkerExit):
        _raise_worker_exit(object(), result, code)
    return capsys.readouterr().err


def test_interrupted_worker_logs_its_stop_reason_before_the_trailer(monkeypatch, capsys):
    reason = "interrupted_by_system(session_turn_lease_lost)"
    err = _exit_stderr(monkeypatch, capsys, {"interrupted": True, "turn_exit_reason": reason}, 130)
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert lines[-2] == f"[kanban-worker-exit] interrupted: {reason}"
    assert lines[-1] == f"{KANBAN_WORKER_EXIT_TRAILER}130"


def test_completed_worker_trailer_is_unchanged(monkeypatch, capsys):
    err = _exit_stderr(monkeypatch, capsys, {"completed": True, "turn_exit_reason": "text_response"}, 0)
    assert [ln for ln in err.splitlines() if ln.strip()] == [f"{KANBAN_WORKER_EXIT_TRAILER}0"]
