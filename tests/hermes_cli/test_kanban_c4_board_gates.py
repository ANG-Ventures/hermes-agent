"""FleetReview retro-backfill C4 (authz / ownership / guard bypass), kanban
board gates slice (t_027d7fe7). One regression per confirmed instance; each is
RED on base 5a9d284d49 and GREEN with the fix, with a control that shows the
gate is not passing by wedging everything.

Instances covered here: #1021 (termination on missing spawn evidence), #1034
(malformed survivor row read before the HOLD backstop), #1074 (env profile
outranking the caller session's owner profile), #999 (dashboard exit from a
claimed review run), #951 (gateway sessionless slash command borrowing the
process-global session id), #960 (goal-mode CLI child treated as operator).
#999 can't, #956, #1081 and #1234 live beside their existing suites.
"""

from __future__ import annotations

import contextvars
import os
import signal
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


# --- #1021: no spawn evidence must never authorize a signal ----------------


def _terminate(monkeypatch, owner_window):
    sent = []
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    # The real identity probe (conftest otherwise lets a stubbed _pid_alive
    # vouch for identity): the PID is this live test process.
    monkeypatch.setattr(kb, "_pid_started_in_claim", kb._real_pid_started_in_claim)
    monkeypatch.setattr(kb.time, "sleep", lambda _s: None)
    host = kb._claimer_id().split(":", 1)[0]
    info = kb._terminate_reclaimed_worker(
        os.getpid(), f"{host}:1", owner_window=owner_window,
        signal_fn=lambda pid, sig: sent.append((pid, sig)),
    )
    return info, sent


def test_missing_spawn_evidence_is_unverified_and_never_signalled(monkeypatch):
    """Only the claim lower bound (epoch 0 here) is known: any process created
    after the claim -- e.g. one that reused the worker PID -- would read
    'verified' and get SIGTERM. It must be unverified and held instead."""
    info, sent = _terminate(monkeypatch, (0.0, None, None))
    assert sent == []
    assert info["owner_identity"] == "unverified"
    assert info["liveness_unprovable"] is True and info["terminated"] is False


def test_bounded_spawn_window_still_signals(monkeypatch):
    """Control: with a spawned upper bound the recorded worker is verified and
    signalled exactly as before."""
    import time as _time

    info, sent = _terminate(monkeypatch, (0.0, _time.time() + 60, None))
    assert info["owner_identity"] == "verified"
    assert sent and sent[0] == (os.getpid(), signal.SIGTERM)


# --- #1034: malformed survivor row must HOLD, never go silent --------------


@pytest.mark.parametrize("survivor,cleanup", [
    ("{", False),   # partially written JSON -> JSONDecodeError in _state()
    ("{", True),
    ("[1]", True),  # valid JSON, not an object -> previous.get AttributeError
])
def test_malformed_survivor_row_holds_with_event(board, survivor, cleanup):
    from hermes_cli import kanban_survivor as ks

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="malformed survivor")
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases, survivor) "
            "VALUES (?, '{}', ?)", (tid, survivor),
        )
        conn.commit()
        with pytest.raises(ks.SurvivorUnavailable) as exc:
            ks.preserve(conn, tid, cleanup=cleanup)
        assert "malformed" in str(exc.value)
        held = conn.execute(
            "SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?", (tid,),
        ).fetchone()[0]
        assert held and "malformed" in held
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
        assert "workspace_held" in kinds


# --- #1074: the caller SESSION's profile outranks the env profile ----------


def _session_db(path: Path, sid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sessions(id) VALUES (?)", (sid,))
        conn.commit()
    finally:
        conn.close()


WORKER_SID = "20260927_000001_worker"


@pytest.fixture
def worker_session(board, monkeypatch):
    """A session that belongs to profile 'daedalus', whose CLI resolves its
    env profile as 'default' after repointing the home at the root board."""
    _session_db(board / "profiles" / "daedalus" / "state.db", WORKER_SID)
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    return WORKER_SID


def _foreign_card(conn, assignee):
    tid = kb.create_task(conn, title="foreign", assignee=assignee,
                         session_id="20260927_999999_home")
    assert kb.block_task(conn, tid, reason="needs input")
    return tid


def test_root_repoint_default_profile_cannot_mutate_default_card(worker_session):
    with kb.connect_closing() as conn:
        tid = _foreign_card(conn, "default")
        with kb.mutation_actor(session_ids=(worker_session,), profile="default"):
            with pytest.raises(kb.ForeignSessionMutationError):
                kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"


def test_root_repoint_default_profile_cannot_use_operator(worker_session):
    with kb.connect_closing() as conn:
        tid = _foreign_card(conn, "builder")
        with kb.mutation_actor(session_ids=(worker_session,), profile="default",
                               operator="Ace via Apollo: ruled"):
            with pytest.raises(kb.ForeignSessionMutationError) as exc:
                kb.unblock_task(conn, tid)
        assert "daedalus" in str(exc.value)
        assert kb.get_task(conn, tid).status == "blocked"


def test_session_owner_profile_is_still_the_assignee(worker_session):
    """Control: the session's real profile keeps assignee authority."""
    with kb.connect_closing() as conn:
        tid = _foreign_card(conn, "daedalus")
        with kb.mutation_actor(session_ids=(worker_session,), profile="default"):
            assert kb.unblock_task(conn, tid)


def test_env_profile_used_when_no_session_owner_found(board, monkeypatch):
    """Control: an unknown session (no state.db row) falls back to the env
    profile exactly as before."""
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    with kb.connect_closing() as conn:
        tid = _foreign_card(conn, "worker-a")
        with kb.mutation_actor(session_ids=("20260927_000002_unknown",),
                               profile="worker-a"):
            assert kb.unblock_task(conn, tid)


# --- #999 (dashboard): a claimed review run leaves only via a verdict -----


def _claimed_review(conn):
    tid = kb.create_task(conn, title="review me", assignee="builder")
    impl = kb.claim_task(conn, tid, claimer="builder:1")
    assert impl is not None
    assert kb.request_review(conn, tid, summary="ready", reviewer="argus",
                             expected_run_id=impl.current_run_id, force=True)
    assert kb.claim_review_task(conn, tid, claimer="reviewer:1") is not None
    assert kb.get_task(conn, tid).status == "running"
    return tid


@pytest.fixture
def dashboard(board, monkeypatch):
    from plugins.kanban.dashboard import plugin_api as api

    calls = []

    def _killed(pid, lock, **_kw):
        calls.append(pid)
        return {"prev_pid": pid, "prev_lock": lock, "host_local": True,
                "termination_attempted": True, "terminated": True, "sigkill": False}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _killed)
    return api, calls


@pytest.mark.parametrize("target", ["todo", "triage", "scheduled"])
def test_dashboard_cannot_park_a_claimed_review_run(dashboard, target):
    api, calls = dashboard
    with kb.connect_closing() as conn:
        tid = _claimed_review(conn)
        assert api._set_status_direct(conn, tid, target) is False
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert calls == []  # refused before the reviewer worker is touched


def test_dashboard_ready_on_claimed_review_resumes_review(dashboard):
    """Control: running -> ready still returns the card to review."""
    api, _calls = dashboard
    with kb.connect_closing() as conn:
        tid = _claimed_review(conn)
        assert api._set_status_direct(conn, tid, "ready") is True
        assert kb.get_task(conn, tid).status == "review"


def test_dashboard_can_still_park_an_implementer_run(dashboard):
    """Control: a normal (non-review) running card can still be moved."""
    api, _calls = dashboard
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="impl", assignee="builder")
        assert kb.claim_task(conn, tid, claimer="builder:1") is not None
        assert api._set_status_direct(conn, tid, "todo") is True
        assert kb.get_task(conn, tid).status == "todo"


# --- #951: a sessionless gateway slash command borrows no identity --------


def test_gateway_sessionless_caller_does_not_inherit_env_session(monkeypatch):
    from hermes_cli import kanban as kc

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SESSION_ID", "20260927_000003_other_chat")
    # The gateway PROCESS (gateway.run imported); a terminal subprocess that
    # only inherited the marker resolves its own bridged env (t_0485b3ff).
    import sys
    import types

    fake_run = types.ModuleType("gateway.run")
    runner = object()
    fake_run._gateway_runner_ref = lambda: runner  # a live GatewayRunner
    monkeypatch.setitem(sys.modules, "gateway.run", fake_run)
    # A fresh context: no per-turn session bound, no explicit slash session.
    assert contextvars.Context().run(kc._caller_session_id) is None


def test_cli_that_merely_imported_gateway_run_is_not_the_gateway(monkeypatch):
    """FleetReview 09c07e5eb0a9: gateway.run sets the marker at import time and
    CLI tools import it lazily. Without a live runner (or the gateway PID being
    ours) the process is a CLI and its env session is its own."""
    import os
    import sys
    import types

    from gateway import status as gw_status
    from hermes_cli import kanban as kc
    from hermes_cli import kanban_db as kdb

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SESSION_ID", "20260928_000001_cli_own")
    fake_run = types.ModuleType("gateway.run")
    fake_run._gateway_runner_ref = lambda: None  # no GatewayRunner here
    monkeypatch.setitem(sys.modules, "gateway.run", fake_run)
    monkeypatch.setattr(gw_status, "get_running_pid", lambda *a, **k: None)

    assert kdb._process_is_gateway() is False
    assert contextvars.Context().run(kc._caller_session_id) == "20260928_000001_cli_own"
    assert contextvars.Context().run(kdb._event_actor)[1] == "20260928_000001_cli_own"

    # Positive ownership via the gateway PID record counts as the gateway.
    monkeypatch.setattr(gw_status, "get_running_pid", lambda *a, **k: os.getpid())
    assert kdb._process_is_gateway() is True
    assert contextvars.Context().run(kc._caller_session_id) is None


def test_gateway_bound_session_and_cli_env_still_resolve(monkeypatch):
    """Controls: the bound per-turn session wins in the gateway; outside the
    gateway the process env is the caller's own."""
    from gateway import session_context as sc
    from hermes_cli import kanban as kc

    monkeypatch.setenv("HERMES_SESSION_ID", "20260927_000004_env")

    def bound():
        sc._SESSION_ID.set("20260927_000005_mine")
        return kc._caller_session_id()

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    assert contextvars.Context().run(bound) == "20260927_000005_mine"
    monkeypatch.delenv("_HERMES_GATEWAY")
    assert contextvars.Context().run(kc._caller_session_id) == "20260927_000004_env"


# --- #960: a goal-mode worker's CLI child is not an operator ---------------


def _raising_judge(**_kw):
    raise RuntimeError("judge transport down")


def _gate(task_id):
    from hermes_cli import goals

    task = SimpleNamespace(goal_mode=True, title="goal card", body="")
    return goals.kanban_handoff_rejection(
        task, "evidence", conn=None, task_id=task_id,
        worker_run_id_for=lambda _tid: None,  # CLI child: never the run owner
        judge_available=lambda: True, judge=_raising_judge,
    )


def test_goal_worker_cli_child_judge_error_fails_closed(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_goalcard")
    reason = _gate("t_goalcard")
    assert reason and "judge error" in reason and "refused" in reason


def test_operator_judge_error_still_fails_open(monkeypatch):
    """Control: a real operator (no inherited worker env for this card)."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert _gate("t_goalcard") is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_some_other_card")
    assert _gate("t_goalcard") is None
