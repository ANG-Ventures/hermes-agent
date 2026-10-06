"""Reassigning a review card with an open own PR implies request-changes (t_dadfedb2).

t_947cea0e (2026-09-29): an operator posted CHANGES REQUESTED as a comment and
ran ``assign <card> daedalus``. The card stayed in ``review``, no worker was
dispatched (task_runs 15671/15779 only), and the changes request had no owner.
``assign``/``reassign`` of a ``review`` card whose own PR is OPEN (or unreadable)
is now refused unless ``--request-changes "<reason>"`` is given; with it the
card goes through ``request_changes`` (changes_requested + ``operator``, ready,
implementer restored).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

from hermes_cli import kanban_open_pr as _open_pr

PR = "https://github.com/ANG-Ventures/fleetreview-router/pull/222"


def _state(state):
    return lambda repo, number: {"state": state}


OPEN = _state("OPEN")


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    # The caller stands in for Apollo: prove it the way production does (the
    # operator gateway's own process tree), not by the env name (t_3b9dbdb1).
    from hermes_cli import kanban_identity as _ki
    monkeypatch.setattr(_ki, "_runs_under_operator_gateway", lambda name: True)
    # The caller is NOT a dispatched worker: no task/run identity.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _review_with_pr(conn, pr=PR) -> str:
    tid = kb.create_task(conn, title="review with open PR", assignee="builder")
    impl = kb.claim_task(conn, tid, claimer="builder:1")
    assert impl is not None
    assert kb.request_review(
        conn, tid, summary="ready", reviewer="human",
        metadata={"pr_url": pr} if pr else None,
        expected_run_id=impl.current_run_id,
    ) is True
    assert kb.get_task(conn, tid).status == "review"
    return tid


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else None
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (tid, kind),
        ).fetchall()
    ]


def test_assign_review_card_with_open_pr_is_refused(board: Path) -> None:
    with kb.connect() as conn:
        tid = _review_with_pr(conn)
        with pytest.raises(kb.ReviewHoldRequired) as exc:
            kb.assign_task(conn, tid, "builder", pr_query=OPEN)
        assert "ANG-Ventures/fleetreview-router#222" in str(exc.value)
        assert "--request-changes" in str(exc.value)
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("review", "human")
        assert _events(conn, tid, "assigned") == []


def test_unreadable_pr_fails_closed(board: Path) -> None:
    def boom(repo, number):
        raise RuntimeError("gh read cap")

    with kb.connect() as conn:
        tid = _review_with_pr(conn)
        with pytest.raises(kb.ReviewHoldRequired):
            kb.assign_task(conn, tid, "builder", pr_query=boom)


def test_request_changes_flag_routes_through_request_changes(board: Path) -> None:
    with kb.connect() as conn:
        tid = _review_with_pr(conn)
        assert kb.assign_task(
            conn, tid, "builder", request_changes_reason="exclude the exhausted family",
            operator="apollo", pr_query=OPEN,
        ) is True
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee, task.claim_lock) == ("ready", "builder", None)
        [changed] = _events(conn, tid, "changes_requested")
        assert changed["reason"] == "exclude the exhausted family"
        assert changed["implementer"] == "builder"
        assert changed["status"] == "ready"
        # hermes-home changes_hold.py counts it as an operator hold only with this key.
        assert changed["operator"].startswith("apollo: ")
        runs = kb.list_runs(conn, tid)
        assert runs[-1].outcome == "changes_requested"


def test_request_changes_flag_to_other_profile_reassigns_after(board: Path) -> None:
    with kb.connect() as conn:
        tid = _review_with_pr(conn)
        assert kb.assign_task(
            conn, tid, "daedalus", request_changes_reason="take it over",
            operator="apollo", pr_query=OPEN,
        ) is True
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("ready", "daedalus")
        assert len(_events(conn, tid, "changes_requested")) == 1
        # Upstream's ``assigned`` payload also carries ``from`` (the respawn guard
        # tells a real dev->closer handoff from a no-op re-assign by it).
        assert _events(conn, tid, "assigned")[-1] == {"assignee": "daedalus", "from": "builder"}


def test_merged_pr_or_no_pr_or_not_review_is_unchanged(board: Path) -> None:
    with kb.connect() as conn:
        merged = _review_with_pr(conn)
        assert kb.assign_task(conn, merged, "builder", pr_query=_state("MERGED")) is True
        assert kb.get_task(conn, merged).status == "review"
        no_pr = _review_with_pr(conn, pr=None)
        assert kb.assign_task(conn, no_pr, "builder", pr_query=OPEN) is True
        plain = kb.create_task(conn, title="plain", assignee="builder")
        assert kb.assign_task(conn, plain, "other", pr_query=OPEN) is True
        assert kb.get_task(conn, plain).assignee == "other"


def test_request_changes_flag_off_review_is_refused(board: Path) -> None:
    with kb.connect() as conn:
        plain = kb.create_task(conn, title="plain", assignee="builder")
        with pytest.raises(kb.ReviewHoldRequired, match="not in an active review run"):
            kb.assign_task(conn, plain, "other", request_changes_reason="x", operator="apollo")
        assert kb.get_task(conn, plain).assignee == "builder"


def test_reassign_receipt_carries_the_hold(board: Path) -> None:
    with kb.connect() as conn:
        tid = _review_with_pr(conn)
        receipt: dict = {}
        assert kb.reassign_task(conn, tid, "builder", receipt=receipt, pr_query=OPEN) is False
        assert "--request-changes" in receipt["hold_error"]
        assert kb.get_task(conn, tid).status == "review"


@pytest.mark.parametrize("verb", ["assign", "reassign"])
def test_cli_assign_and_reassign(board: Path, monkeypatch, capsys, verb) -> None:
    monkeypatch.setattr(_open_pr, "_default_query", lambda: OPEN)
    with kb.connect() as conn:
        tid = _review_with_pr(conn)
    cmd = kc._cmd_assign if verb == "assign" else kc._cmd_reassign
    base = dict(task_id=tid, profile="builder", reclaim=False, reason=None)
    assert cmd(argparse.Namespace(**base, request_changes=None)) == 1
    assert "--request-changes" in capsys.readouterr().err
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "review"
    assert cmd(argparse.Namespace(**base, request_changes="exclude exhausted family")) == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("ready", "builder")
        [changed] = _events(conn, tid, "changes_requested")
        assert changed["operator"].startswith("apollo: ")


@pytest.mark.parametrize("verb", ["assign", "reassign"])
def test_cli_assign_on_review_card_warns_it_stays_parked(board: Path, monkeypatch, capsys, verb) -> None:
    """t_78ea7580: assign on a review card succeeds but nothing dispatches it: say so, name the verb.

    Measured 2026-10-02/03 (t_3bd70b59, t_a57274a4, t_82169667): an operator
    posted GO + ``assign <card> <worker>``; the cards sat in ``review`` 4-8 h
    with no worker. The assign still exits 0 (unchanged behaviour); the
    WARNING on stderr names the status and ``request-changes --coverage``.
    """
    monkeypatch.setattr(_open_pr, "_default_query", lambda: _state("MERGED"))
    with kb.connect() as conn:
        tid = _review_with_pr(conn)
    cmd = kc._cmd_assign if verb == "assign" else kc._cmd_reassign
    base = dict(task_id=tid, profile="daedalus", reclaim=False, reason=None, request_changes=None)
    assert cmd(argparse.Namespace(**base)) == 0
    out, err = capsys.readouterr()
    assert tid in out
    assert "WARNING" in err and "'review'" in err and "does NOT dispatch" in err
    assert f"request-changes {tid}" in err and "--coverage" in err
    assert f"complete {tid}" in err
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("review", "daedalus")

    # A human sentinel on a review card is a deliberate parked lane: no warning.
    assert cmd(argparse.Namespace(**dict(base, profile="human:apollo"))) == 0
    assert "WARNING" not in capsys.readouterr().err
    # A card that is not in review dispatches normally: no warning.
    with kb.connect() as conn:
        plain = kb.create_task(conn, title="plain", assignee="builder")
    assert cmd(argparse.Namespace(**dict(base, task_id=plain))) == 0
    assert "WARNING" not in capsys.readouterr().err


def test_triage_resolve_on_review_card_names_request_changes(board: Path) -> None:
    """t_78ea7580: the refusal names the working verb, not just 'complete'."""
    with kb.connect() as conn:
        tid = _review_with_pr(conn)
        ok, err = kb.triage_resolve_task(conn, tid, to="todo", reason="re-queue", actor="apollo")
    assert ok is False
    assert "is 'review'" in err
    assert "request-changes <id>" in err and "--coverage" in err
    assert "complete <id>" in err
    assert "assign/reassign alone leaves it in review" in err
