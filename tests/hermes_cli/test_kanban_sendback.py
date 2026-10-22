"""Forge L0 SB-K: the kernel ``sendback`` handoff kind (spec §2.4).

A producer (CI red, Prism, lander, conflict, revert) sends a PR defect straight
back to the card's implementer. The kernel contract pinned here: ``sendback``
is a handoff kind the respawn guard and dispatcher treat exactly like
``changes_requested``, and its payload reaches the next worker prompt.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR = "https://github.com/example/repo/pull/44"


def _payload(**over):
    p = {
        "v": 1,
        "key": "example/repo#44@" + "a" * 40 + ":ci-red:lints-fast",
        "repo": "example/repo", "pr": 44, "head": "a" * 40,
        "source": "ci-red", "reason": "ci-red:lints-fast", "retry": "after_change",
        "summary": "lints-fast red on your diff: tests/test_x.py::test_y",
        "evidence": {"failing_ids": ["tests/test_x.py::test_y"]},
        "round": 1,
    }
    p.update(over)
    return p


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(kb, "_query_github_pr_state", lambda repo, number: "OPEN")
    clock = {"now": int(time.time())}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
    kb.init_db()
    return SimpleNamespace(path=home, clock=clock)


def _tick(home, s=5):
    home.clock["now"] += s


def _stopped_in_review(conn, home, title):
    """Implementer opened a PR, requested review and exited (no live worker)."""
    tid = kb.create_task(conn, title=title, assignee="dev")
    claimed = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
    _tick(home)
    assert kb.request_review(
        conn, tid, summary="PR ready", reviewer="human",
        expected_run_id=claimed.current_run_id,
    )
    _tick(home)
    return tid


def _spawned_ids(conn):
    spawned: list[str] = []
    kbd.dispatch_once(
        conn, spawn_fn=lambda task, workspace, board=None: (spawned.append(task.id) or 4242),
    )
    return spawned


def test_kernel_sendback_is_handoff_kind(kanban_home: SimpleNamespace) -> None:
    """``sendback`` sits in both handoff kind sets ``changes_requested`` is in,
    and lifts ``active_pr`` on a ready card with an open PR for one spawn."""
    assert "sendback" in kb._RESPAWN_GUARD_OPERATOR_REQUEUE_KINDS
    assert kbd._is_handoff_event("sendback", None) is True
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ready with PR", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR}")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kbd.check_respawn_guard(conn, tid) is None
        assert tid in _spawned_ids(conn)
        # Consumed by that spawn, exactly like an operator requeue.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL, "
                "worker_pid=NULL WHERE id=?", (tid,),
            )
        # The spawn consumed the requeue intent, but the handoff itself still
        # names the implementer until it posts a newer PR comment (#111910).
        assert kbd.check_respawn_guard(conn, tid) is None
        _tick(kanban_home)
        kb.add_comment(conn, tid, author="dev", body=f"Pushed fix to {PR}")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("kind", ["changes_requested", "sendback"])
def test_handoff_survives_consumed_intent_like_changes_requested(
    kanban_home: SimpleNamespace, kind: str,
) -> None:
    """Both kinds lift ``active_pr`` through the handoff query, not only the
    one-shot operator intent a spawn consumes."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=kind, assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR}")
        _tick(kanban_home)
        kb._append_event(conn, tid, kind, {"reason": "fix"})
        kb._append_event(conn, tid, "spawned", {"pid": 99999999})
        assert not kb._unused_operator_intent_after_pr(conn, tid)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_sendback_respawns_stopped_worker_exactly_as_changes_requested(
    kanban_home: SimpleNamespace,
) -> None:
    from tests.kanban_review_helpers import covered_request_changes

    with kbc.connect() as conn:
        a = _stopped_in_review(conn, kanban_home, "changes requested")
        b = _stopped_in_review(conn, kanban_home, "sendback")

        rclaim = kb.claim_review_task(conn, a)
        ok, who = covered_request_changes(
            conn, a, reason="fix lints", expected_run_id=rclaim.current_run_id,
        )
        assert (ok, who) == (True, "dev")
        ok, delivery = kb.request_sendback(conn, b, payload=_payload())
        assert (ok, delivery) == (True, "requeued")

        for tid in (a, b):
            t = kb.get_task(conn, tid)
            assert (t.status, t.assignee, t.current_run_id) == ("ready", "dev", None)
            assert kbd.check_respawn_guard(conn, tid) is None
        spawned = _spawned_ids(conn)
        assert a in spawned and b in spawned


def test_sendback_payload_renders_into_worker_prompt(kanban_home: SimpleNamespace) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="prompt", assignee="dev")
        assert "Send-backs on your PR" not in kb.build_worker_context(conn, tid)
        kb.request_sendback(conn, tid, payload=_payload())
        ctx = kb.build_worker_context(conn, tid)
    assert "## Send-backs on your PR" in ctx
    assert "ci-red: ci-red:lints-fast r1 on example/repo#44 @ aaaaaaaaaaaa" in ctx
    assert "lints-fast red on your diff" in ctx
    assert "tests/test_x.py::test_y" in ctx


def test_sendback_to_running_implementer_is_in_flight(kanban_home: SimpleNamespace) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="running", assignee="dev")
        run = kb.claim_task(conn, tid)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "in_flight")
        t = kb.get_task(conn, tid)
        assert (t.status, t.current_run_id) == ("running", run.current_run_id)
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "sendback"]
        assert len(ev) == 1 and ev[0].payload["delivery"] == "in_flight"


def test_sendback_refusals_write_nothing(kanban_home: SimpleNamespace) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="refuse", assignee="dev")
        for bad in (
            _payload(source="apollo"),
            _payload(retry="operator"),
            _payload(summary=" "),
            "not a dict",
        ):
            ok, _ = kb.request_sendback(conn, tid, payload=bad)  # type: ignore[arg-type]
            assert not ok
        # An active review run belongs to the reviewer, not the implementer.
        r = _stopped_in_review(conn, kanban_home, "in review")
        kb.claim_review_task(conn, r)
        assert kb.request_sendback(conn, r, payload=_payload())[0] is False
        assert kb.request_sendback(conn, "t_nope", payload=_payload()) == (False, "task not found")
        for t in (tid, r):
            assert not [e for e in kb.list_events(conn, t) if e.kind == "sendback"]


@pytest.mark.parametrize("prior_round", [False, True])
def test_rereview_after_sendback_keeps_reviewer_provenance(
    kanban_home: SimpleNamespace, prior_round: bool,
) -> None:
    """Reviewer provenance is read from the newest handoff event of either
    kind: review -> sendback -> request_review(reviewer=None) re-routes to the
    original reviewer, with or without an earlier changes_requested round."""
    from tests.kanban_review_helpers import covered_request_changes

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rereview", assignee="dev")
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="PR ready", reviewer="human:apollo",
            expected_run_id=claimed.current_run_id,
        )
        if prior_round:
            rclaim = kb.claim_review_task(conn, tid)
            assert covered_request_changes(
                conn, tid, reason="round 1", expected_run_id=rclaim.current_run_id,
            )[0]
            again = kb.claim_task(conn, tid)
            _tick(kanban_home)
            assert kb.request_review(
                conn, tid, summary="fixed", expected_run_id=again.current_run_id,
            )
        _tick(kanban_home)
        # A producer cannot forge handoff provenance.
        spoof = _payload(reviewer="mallory", implementer="mallory")
        assert kb.request_sendback(conn, tid, payload=spoof) == (True, "requeued")
        sb = [e for e in kb.list_events(conn, tid) if e.kind == "sendback"][-1]
        assert (sb.payload["reviewer"], sb.payload["implementer"]) == ("human:apollo", "dev")
        fix = kb.claim_task(conn, tid)
        assert "## Send-backs on your PR" in kb.build_worker_context(conn, tid)
        _tick(kanban_home)
        assert kb.request_review(
            conn, tid, summary="fixed sendback", expected_run_id=fix.current_run_id,
        )
        rr = [e for e in kb.list_events(conn, tid) if e.kind == "review_requested"][-1]
        assert rr.payload["reviewer"] == "human:apollo"
        # Answered by that handback: not replayed into the next prompt.
        assert "## Send-backs on your PR" not in kb.build_worker_context(conn, tid)
