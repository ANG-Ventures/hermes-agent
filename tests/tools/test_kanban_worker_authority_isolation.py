"""Regression tests: Kanban worker authority must not cross a process boundary.

A dispatcher-spawned worker's ``HERMES_KANBAN_*`` environment is inherited by
every child process it launches. Inheriting that env used to be indistinguishable
from *being* the worker, so an ordinary nested ``hermes chat`` fired from a
worker's own shell saw the Kanban toolset and called ``kanban_complete`` on its
parent's card — writing an unrelated summary as the card's durable terminal
result while the owning worker was still running (2026-08-12, card t_09b90233).

The authority anchor is ``HERMES_KANBAN_OWNER_PID``: the dispatcher stamps a
single-use ``pending`` sentinel, the booting worker CLI binds it to its own pid,
and every later process in the tree inherits a pid that is not its own.

These tests assert both directions — the nested child is refused, and the
genuine dispatcher worker remains fully authorized.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_running_kanban_task(monkeypatch, tmp_path):
    """Create + claim a task on an ISOLATED board and return worker env facts."""
    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "worker-profile")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="owner card",
            assignee="worker-profile",
            workspace_kind="scratch",
            workspace_path=str(workspace),
        )
        claim = kb.claim_task(conn, tid)
        assert claim is not None
        run_id = claim.id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim.claim_lock or "lock")
    return kb, tid


def _become_the_worker():
    """Claim the dispatcher's grant for THIS process, as CLI startup does."""
    from agent.delegation_context import (
        KANBAN_OWNER_PID_ENV,
        KANBAN_OWNER_PID_PENDING,
        claim_kanban_worker_authority,
    )

    os.environ[KANBAN_OWNER_PID_ENV] = KANBAN_OWNER_PID_PENDING
    assert claim_kanban_worker_authority() is True
    assert os.environ[KANBAN_OWNER_PID_ENV] == str(os.getpid())


# --------------------------------------------------------------------------
# The predicate itself
# --------------------------------------------------------------------------

def test_grant_binds_to_exactly_one_process(monkeypatch):
    """A claimed grant authorizes its owner and nothing else."""
    from agent.delegation_context import (
        KANBAN_OWNER_PID_ENV,
        KANBAN_OWNER_PID_PENDING,
        claim_kanban_worker_authority,
        owns_kanban_worker_authority,
    )

    # Absent marker fails OPEN: hand-driven workers / older dispatchers.
    monkeypatch.delenv(KANBAN_OWNER_PID_ENV, raising=False)
    assert owns_kanban_worker_authority() is True

    # An issued-but-unclaimed grant belongs to nobody yet.
    monkeypatch.setenv(KANBAN_OWNER_PID_ENV, KANBAN_OWNER_PID_PENDING)
    assert owns_kanban_worker_authority() is False

    # Claiming binds it to this pid, and is idempotent.
    assert claim_kanban_worker_authority() is True
    assert os.environ[KANBAN_OWNER_PID_ENV] == str(os.getpid())
    assert owns_kanban_worker_authority() is True
    assert claim_kanban_worker_authority() is True

    # A grant naming another process is not ours, and cannot be re-claimed.
    monkeypatch.setenv(KANBAN_OWNER_PID_ENV, str(os.getpid() + 1))
    assert owns_kanban_worker_authority() is False
    assert claim_kanban_worker_authority() is False
    assert os.environ[KANBAN_OWNER_PID_ENV] == str(os.getpid() + 1)

    # A corrupt marker is refused rather than guessed at.
    monkeypatch.setenv(KANBAN_OWNER_PID_ENV, "not-a-pid")
    assert owns_kanban_worker_authority() is False


def test_dispatcher_stamps_pending_grant_on_worker_spawn(monkeypatch, tmp_path):
    """The dispatcher must issue the grant, or nothing downstream can bind it."""
    from agent.delegation_context import (
        KANBAN_OWNER_PID_ENV,
        KANBAN_OWNER_PID_PENDING,
    )
    from hermes_cli import kanban_db as kb

    captured = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return _FakeProc()

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="spawn me", assignee="worker-profile")
        task = kb.get_task(conn, tid)
        assert task is not None
    finally:
        conn.close()

    workspace = tmp_path / "ws"
    workspace.mkdir()
    kb._default_spawn(task, str(workspace))

    assert captured["env"][KANBAN_OWNER_PID_ENV] == KANBAN_OWNER_PID_PENDING
    assert captured["env"]["HERMES_KANBAN_TASK"] == tid


# --------------------------------------------------------------------------
# The real process boundary
# --------------------------------------------------------------------------

_CHILD_PROBE = r"""
import json, os, sys
import tools.kanban_tools as kt

print("PROBE " + json.dumps({
    "tools_exposed": bool(kt._check_kanban_mode()),
    "default_task_id": kt._default_task_id(None),
    "complete": str(kt._handle_complete(
        {"task_id": %(tid)r, "summary": "SENTINEL-FROM-NESTED-CHILD"}
    )),
}))
"""


def _run_nested_child(tid: str, env: dict, cwd: Path) -> dict:
    """Run a REAL child process that inherits the worker's environment."""
    child_env = dict(env)
    child_env["PYTHONPATH"] = str(_REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE % {"tid": tid}],
        capture_output=True,
        text=True,
        env=child_env,
        cwd=str(cwd),
    )
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE "):
            return json.loads(line[len("PROBE "):])
    raise AssertionError(
        f"child probe produced no result\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_nested_process_cannot_see_or_mutate_the_parents_card(monkeypatch, tmp_path):
    """The vulnerable path, at the real process boundary.

    An ordinary child process inheriting the full ``HERMES_KANBAN_*`` set must
    get no Kanban tools AND be refused if it calls a handler directly. Both
    halves matter: the schema gate hides the tools from a model, but a direct
    handler call bypasses the schema entirely, so the write path needs its own
    check.
    """
    kb, tid = _make_running_kanban_task(monkeypatch, tmp_path)
    _become_the_worker()

    payload = _run_nested_child(tid, dict(os.environ), tmp_path)

    assert payload["tools_exposed"] is False
    assert payload["default_task_id"] is None
    assert "refusing to mutate" in payload["complete"]
    assert "does not own it" in payload["complete"]

    # The durable board record is what actually got corrupted in the incident.
    conn = kb.connect()
    try:
        after = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
    finally:
        conn.close()

    assert after.status == "running"
    assert after.result is None
    assert not any("SENTINEL" in (r.summary or "") for r in runs)


def test_genuine_dispatcher_worker_remains_authorized(monkeypatch, tmp_path):
    """Positive control: the fix must not lock out the real worker."""
    kb, tid = _make_running_kanban_task(monkeypatch, tmp_path)
    _become_the_worker()

    import tools.kanban_tools as kt

    assert kt._check_kanban_mode() is True
    assert kt._default_task_id(None) == tid
    assert kt._enforce_worker_task_ownership(tid) is None

    result = kt._handle_complete({"summary": "GENUINE-WORKER-HANDOFF"})
    assert '"ok": true' in result

    conn = kb.connect()
    try:
        after = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
    finally:
        conn.close()

    assert after.status == "done"
    assert any("GENUINE-WORKER-HANDOFF" in (r.summary or "") for r in runs)


def test_worker_without_grant_is_unaffected(monkeypatch, tmp_path):
    """Back-compat: no grant in env ⇒ legacy behaviour, still authorized.

    Covers hand-driven ``HERMES_KANBAN_TASK=... hermes chat`` runs and any
    dispatcher older than the stamp.
    """
    from agent.delegation_context import KANBAN_OWNER_PID_ENV

    kb, tid = _make_running_kanban_task(monkeypatch, tmp_path)
    monkeypatch.delenv(KANBAN_OWNER_PID_ENV, raising=False)

    import tools.kanban_tools as kt

    assert kt._check_kanban_mode() is True
    assert kt._default_task_id(None) == tid
    assert kt._enforce_worker_task_ownership(tid) is None


# --------------------------------------------------------------------------
# Recovery from a false terminal state
# --------------------------------------------------------------------------

def test_reopen_voids_a_false_completion_and_requeues(monkeypatch, tmp_path):
    """A bogus ``done`` must be reversible — comments cannot fix task status.

    The incident left a card terminal with an impostor's summary as its
    durable result. ``reopen`` clears the false result, marks the closing run
    ``voided`` (rather than deleting the evidence), and returns the card to
    the queue so the real work can be re-dispatched.
    """
    kb, tid = _make_running_kanban_task(monkeypatch, tmp_path)

    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="BOGUS-SENTINEL")
        assert kb.get_task(conn, tid).status == "done"

        # A comment does not change status or result — the whole reason this
        # recovery path has to exist.
        kb.add_comment(conn, tid, "operator", "this completion is false")
        assert kb.get_task(conn, tid).status == "done"

        ok, err = kb.reopen_task(
            conn, tid, actor="operator", reason="false completion by a non-owner",
        )
        assert ok is True and err is None

        after = kb.get_task(conn, tid)
        assert after.status == "ready"
        assert after.result is None
        assert after.completed_at is None
        assert after.claim_lock is None

        runs = kb.list_runs(conn, tid)
        assert any(r.outcome == "voided" for r in runs)
        # The bogus summary is retained on the voided run as audit evidence.
        assert any("BOGUS-SENTINEL" in (r.summary or "") for r in runs)

        events = [e for e in kb.list_events(conn, tid) if e.kind == "reopened"]
        assert len(events) == 1
        assert events[0].payload["reason"] == "false completion by a non-owner"
        assert events[0].payload["actor"] == "operator"
    finally:
        conn.close()


def test_reopen_refuses_non_done_tasks_and_anonymous_reversals(monkeypatch, tmp_path):
    """Guards: only ``done`` is reversible, and never without a reason."""
    kb, tid = _make_running_kanban_task(monkeypatch, tmp_path)

    conn = kb.connect()
    try:
        # Still running — not a terminal state to reverse.
        ok, err = kb.reopen_task(conn, tid, actor="op", reason="nope")
        assert ok is False
        assert "only applies to 'done'" in err

        kb.complete_task(conn, tid, summary="done for real")
        ok, err = kb.reopen_task(conn, tid, actor="op", reason="   ")
        assert ok is False
        assert "reason is required" in err
        assert kb.get_task(conn, tid).status == "done"

        ok, err = kb.reopen_task(conn, tid, actor="op", reason="x", to_status="done")
        assert ok is False
        assert "invalid target status" in err
    finally:
        conn.close()
