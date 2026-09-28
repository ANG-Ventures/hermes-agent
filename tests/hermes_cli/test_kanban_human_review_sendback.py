"""The HUMAN review lane can send a review card back to its implementer (t_088fe9e3).

Under ``review_policy=milestone_only`` with ``review_assignee`` resolving to the
human lane, the operator session (Apollo, D-1) reviews via
``hermes kanban claim <id> --review``. Its later ``comment`` and
``request-changes`` calls are separate CLI processes, so the dispatcher env
never attests to the review run. The claim itself is the provenance: the
session that made it is recorded on the ``claimed`` event, and only that
session (never a delegate child, never a cron job) inherits the run id.
"""
import json
import shlex
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES

SESSION = "20260925_150000_operator"
OTHER_SESSION = "20260925_150000_bystander"


def _coverage_json() -> str:
    return json.dumps({
        "lenses": {k: "done" for k in LENSES},
        "findings": 1, "items": ["Missing guard at handler:42"],
        "review_minutes": 5, "batch_id": "batch-human-1",
        "head_sha": "n/a: fixture card has no PR",
    })


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    import os
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN"):
            monkeypatch.delenv(key)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT", "_HERMES_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        # Homed to the operator session, like a card Apollo files from chat.
        tid = kb.create_task(conn, title="human review", assignee="builder",
                             session_id=SESSION)
        impl = kb.claim_task(conn, tid)
        assert impl
        assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                                 expected_run_id=impl.current_run_id)
    return tid


def _as_session(monkeypatch, session):
    if session is None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_ID", session)


def _claim(monkeypatch, tid, session=SESSION):
    _as_session(monkeypatch, session)
    assert "Claimed" in cli.run_slash(f"claim {tid} --review")
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "running"
    return task.current_run_id


# Refusal probes pass --takeover so the HOME-SESSION guard (a separate gate)
# is satisfied and the review-claim gate is the one under test.
TAKEOVER = '--takeover "probe: review-claim gate"'


def _status(tid):
    with kb.connect() as conn:
        return kb.get_task(conn, tid).status


def test_cli_claim_coverage_comment_then_request_changes_routes_to_ready(board, monkeypatch):
    run_id = _claim(monkeypatch, board)
    cli.run_slash(f"comment {board} {shlex.quote('review_coverage: ' + _coverage_json())}")
    with kb.connect() as conn:
        stamped = [c for c in kb.list_comments(conn, board) if "review_coverage:" in c.body]
    assert stamped and stamped[-1].run_id == run_id

    out = cli.run_slash(f'request-changes {board} "BEHAVIOUR: add the missing guard"')
    assert "Requested changes" in out, out
    with kb.connect() as conn:
        task = kb.get_task(conn, board)
        assert task.status == "ready"
        assert task.assignee == "builder"
        events = [e for e in kb.list_events(conn, board) if e.kind == "changes_requested"]
        assert events and events[-1].payload["reason"] == "BEHAVIOUR: add the missing guard"
        rework = [c for c in kb.list_comments(conn, board)
                  if "BEHAVIOUR: add the missing guard" in c.body]
        assert rework, "reason must land as the implementer's rework comment"


def test_request_changes_coverage_flag_works_for_claim_holder(board, monkeypatch):
    run_id = _claim(monkeypatch, board)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())}'
    )
    assert "Requested changes" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, board).status == "ready"
        assert any(c.run_id == run_id and "batch-human-1" in c.body
                   for c in kb.list_comments(conn, board))


def test_session_without_the_claim_is_refused(board, monkeypatch):
    _claim(monkeypatch, board, SESSION)
    _as_session(monkeypatch, OTHER_SESSION)
    cli.run_slash(f"comment {board} {shlex.quote('review_coverage: ' + _coverage_json())}")
    with kb.connect() as conn:
        assert all(c.run_id is None for c in kb.list_comments(conn, board))
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "cannot request changes" in out and "claim" in out, out
    assert _status(board) == "running"
    with kb.connect() as conn:
        assert all(c.run_id is None for c in kb.list_comments(conn, board))


def _claim_refused(monkeypatch, tid, session):
    """An unbindable caller's ``claim --review`` is refused up front
    (t_c3cf232e): it could never be used, and accepting it only stranded the
    card in ``running`` under the exited CLI's pid."""
    _as_session(monkeypatch, session)
    out = cli.run_slash(f"claim {tid} --review {TAKEOVER}")
    assert "Claimed" not in out and "no session identity" in out, out
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "review" and task.claim_lock is None


def test_sessionless_claim_is_not_bound_and_refused(board, monkeypatch):
    _claim_refused(monkeypatch, board, None)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())}'
    )
    assert "cannot request changes" in out, out
    assert _status(board) == "review"
    # An explicit --takeover is the operator's override: a card parked in
    # review is sent back without any claim (t_c3cf232e scope add).
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "Requested changes" in out, out
    assert _status(board) == "ready"


def test_delegate_child_caller_is_refused_even_with_the_claiming_session(board, monkeypatch):
    _claim(monkeypatch, board, SESSION)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")  # DELEGATED_CHILD_ENV_MARKER
    cli.run_slash(f"comment {board} {shlex.quote('review_coverage: ' + _coverage_json())}")
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "Requested changes" not in out, out
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    assert _status(board) == "running"
    with kb.connect() as conn:
        assert all(c.run_id is None for c in kb.list_comments(conn, board))


def test_delegate_child_cannot_inherit_the_run_via_the_resolver(board, monkeypatch):
    """Direct gate check: the DB-level connect refusal must not be the only wall."""
    run_id = _claim(monkeypatch, board, SESSION)
    with kb.connect() as conn:
        assert cli._operator_review_run_id(conn, board) == run_id
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
        assert cli._operator_review_run_id(conn, board) is None

def test_cron_session_cannot_bind_a_review_claim(board, monkeypatch):
    cron = "cron_abc123_20260925_150000"
    _claim_refused(monkeypatch, board, cron)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "cannot request changes" in out, out
    assert _status(board) == "review"


def test_claim_event_records_session_ref_not_raw_session_id(board, monkeypatch):
    run_id = _claim(monkeypatch, board, SESSION)
    with kb.connect() as conn:
        claimed = [e for e in kb.list_events(conn, board)
                   if e.kind == "claimed" and e.run_id == run_id]
    assert claimed[-1].payload.get("session_ref") == kb.derive_session_ref(SESSION)
    assert SESSION not in json.dumps(claimed[-1].payload)


# --- t_0485b3ff: claim --review + request-changes from a gateway chat turn ---
#
# A chat turn runs ``hermes kanban`` in a terminal SUBPROCESS of the gateway:
# it inherits _HERMES_GATEWAY=1 but gets its own HERMES_SESSION_ID bridged per
# command, and it never imports gateway.run. Before the fix the CLI treated it
# as the in-process gateway, bound no session to the claim, and the following
# request-changes was refused with the card stranded in running.


def _gateway_subprocess_env(monkeypatch, session=SESSION):
    import sys
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    _as_session(monkeypatch, session)


def test_gateway_subprocess_env_claim_then_request_changes_in_one_turn(board, monkeypatch):
    import contextvars
    _gateway_subprocess_env(monkeypatch)
    out = contextvars.Context().run(cli.run_slash, f"claim {board} --review")
    assert "Claimed" in out and "bound no session" not in out, out
    with kb.connect() as conn:
        run_id = kb.get_task(conn, board).current_run_id
        claimed = [e for e in kb.list_events(conn, board)
                   if e.kind == "claimed" and e.run_id == run_id]
    assert claimed[-1].payload.get("session_ref") == kb.derive_session_ref(SESSION)
    out = contextvars.Context().run(
        cli.run_slash,
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}',
    )
    assert "Requested changes" in out, out
    assert _status(board) == "ready"


def test_in_process_gateway_still_borrows_no_env_session(monkeypatch):
    """Control (#951): the gateway process itself still ignores os.environ."""
    import contextvars
    import sys
    import types
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER_SESSION)
    fake_run = types.ModuleType("gateway.run")
    runner = object()
    fake_run._gateway_runner_ref = lambda: runner  # the gateway PROCESS
    monkeypatch.setitem(sys.modules, "gateway.run", fake_run)
    assert contextvars.Context().run(cli._caller_session_id) is None
    monkeypatch.delitem(sys.modules, "gateway.run")
    assert contextvars.Context().run(cli._caller_session_id) == OTHER_SESSION


def _legacy_unbound_claim(tid):
    """An unbound review claim as the pre-t_c3cf232e CLI (or a legacy row)
    left it: ``claim --review`` now refuses a sessionless caller, so build the
    state directly to keep covering ``release_unbound_review_claim``."""
    with kb.connect() as conn:
        task = kb.claim_review_task(conn, tid, session_ref=None)
    assert task is not None and task.status == "running"
    return task.current_run_id


def test_unbound_claim_is_released_and_sent_back_by_a_bindable_session(board, monkeypatch):
    """A claim taken with no session (the pre-fix gateway shape) no longer
    strands: request-changes from a bindable session releases it and sends
    back through the parked-review path in the same call."""
    _legacy_unbound_claim(board)
    _as_session(monkeypatch, SESSION)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "Requested changes" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, board).status == "ready"
        released = [e for e in kb.list_events(conn, board)
                    if e.kind == "reclaimed" and e.payload.get("unbound_review_claim")]
    assert released and released[-1].payload["retry_status"] == "review"


def test_unbound_claim_held_by_a_live_other_process_is_not_released(board, monkeypatch):
    _legacy_unbound_claim(board)
    with kb.connect() as conn:
        host = kb._claimer_id().rpartition(":")[0]
        conn.execute("UPDATE tasks SET claim_lock = ? WHERE id = ?", (f"{host}:1", board))
        conn.commit()
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    _as_session(monkeypatch, SESSION)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "cannot request changes" in out, out
    assert _status(board) == "running"


def test_unbound_claim_held_on_a_remote_host_is_not_released(board, monkeypatch):
    """FleetReview af22d38d1623: a remote lock is not proof its claimer is gone."""
    _legacy_unbound_claim(board)
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET claim_lock = ? WHERE id = ?",
                     ("some-other-host:4242", board))
        conn.commit()
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect() as conn:
        assert kb.release_unbound_review_claim(conn, board, reason="probe") is False
    _as_session(monkeypatch, SESSION)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "Requested changes" not in out, out
    assert _status(board) == "running"


def test_worker_attached_after_the_reads_blocks_the_release(board, monkeypatch):
    """FleetReview cbacb005a819: release conditions are rechecked in the txn."""
    _legacy_unbound_claim(board)
    with kb.connect() as conn:
        host = kb._claimer_id().rpartition(":")[0]
        conn.execute("UPDATE tasks SET claim_lock = ? WHERE id = ?", (f"{host}:1", board))
        conn.commit()

    def _attach_then_report_dead(pid):
        # Another process attaches a worker between the reads and the write.
        with kb.connect() as other:
            other.execute("UPDATE tasks SET worker_pid = 777 WHERE id = ?", (board,))
            other.commit()
        return False

    monkeypatch.setattr(kb, "_pid_alive", _attach_then_report_dead)
    with kb.connect() as conn:
        assert kb.release_unbound_review_claim(conn, board, reason="probe") is False
        task = kb.get_task(conn, board)
    assert task.status == "running" and task.worker_pid == 777


def test_bound_claim_is_never_released_by_another_session(board, monkeypatch):
    _claim(monkeypatch, board, SESSION)
    with kb.connect() as conn:
        assert kb.release_unbound_review_claim(conn, board, reason="probe") is False
    assert _status(board) == "running"
