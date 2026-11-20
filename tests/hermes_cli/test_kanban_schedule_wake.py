"""Timed wake for ``scheduled`` cards + the unwoken-scheduled lint (t_6915068e).

2026-09-26: t_dab7ed4e sat in ``scheduled`` 32h past its date gate. Nothing in
the dispatcher wakes a scheduled card, ``next_eligible_at`` was NULL, and the
operator tried promote / promote --force / reopen / requeue / triage-resolve —
all refuse on ``scheduled``. ``unblock`` was the exit but no refusal named it.

Pinned here:
1. ``schedule --at/--now`` stamps a timed wake; the dispatcher's next tick
   returns the card to ``ready`` (``todo`` while parents are open) and records
   why (``schedule_elapsed``).
2. ``find_unwoken_scheduled`` / ``DispatchResult.unwoken_scheduled`` name a
   NULL-gated scheduled card parked >= 24h; a 1h-old one stays silent.
3. ``promote`` on a scheduled card names the exit verbs.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # ``kanban_db_path`` honours HERMES_KANBAN_DB above HERMES_HOME (the
    # dispatcher→worker handoff pins it). A test inheriting it from a worker
    # env would write to the REAL board, so clear it explicitly.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home



HOUR = 3600


def _args(**kw) -> argparse.Namespace:
    base = {"reason": [], "ids": None, "at": None, "now": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _scheduled(conn, title="gated", parked_seconds_ago: int = 0) -> str:
    tid = kb.create_task(conn, title=title, assignee="worker",
                         body="unblock_when: event:readback")
    assert kb.schedule_task(conn, tid, reason="wait for #978 readback")
    if parked_seconds_ago:
        ts = int(time.time()) - parked_seconds_ago
        with kb.write_txn(conn):
            # Fixture aging only: backdate when the card was parked.
            conn.execute(
                "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='scheduled'",
                (ts, tid),
            )
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (ts, tid))
    assert kb.get_task(conn, tid).status == "scheduled"
    assert kb.get_task(conn, tid).next_eligible_at is None
    return tid


def _kinds(conn, tid) -> list[str]:
    return [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)
        )
    ]


# --- lint -------------------------------------------------------------------


def test_null_gated_scheduled_25h_old_is_reported(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        old = _scheduled(conn, "old", parked_seconds_ago=25 * HOUR)
        res = kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 1)
        assert [tid for tid, _age in res.unwoken_scheduled] == [old]
        assert res.unwoken_scheduled[0][1] >= 25 * HOUR
        line = kb.format_unwoken_scheduled(res.unwoken_scheduled)
        assert old in line and "STRANDED" in line and "unblock" in line


def test_null_gated_scheduled_1h_old_is_silent(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _scheduled(conn, "fresh", parked_seconds_ago=1 * HOUR)
        res = kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 1)
        assert res.unwoken_scheduled == []
        assert kb.format_unwoken_scheduled(res.unwoken_scheduled) == ""


def test_timed_scheduled_card_is_not_reported(kanban_home: Path) -> None:
    """A card WITH a future wake has a trigger; it is not rotting."""
    with kb.connect_closing() as conn:
        tid = _scheduled(conn, "timed", parked_seconds_ago=25 * HOUR)
        ok, err = kb.set_schedule_wake(
            conn, tid, wake_at=int(time.time()) + 10 * HOUR, actor="op",
        )
        assert ok, err
        assert kb.find_unwoken_scheduled(conn) == []


def test_lint_uses_latest_park_not_card_age(kanban_home: Path) -> None:
    """A card created long ago but parked 1h ago is fresh."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="old card", assignee="worker",
                             body="unblock_when: event:readback")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET created_at=? WHERE id=?",
                (int(time.time()) - 72 * HOUR, tid),
            )
        assert kb.schedule_task(conn, tid, reason="x")
        assert kb.find_unwoken_scheduled(conn) == []
        assert kb.find_unwoken_scheduled(conn, threshold_seconds=0)[0][0] == tid


# --- timed wake -------------------------------------------------------------


def test_cli_schedule_now_wakes_on_next_tick(kanban_home: Path, capsys) -> None:
    with kb.connect_closing() as conn:
        tid = _scheduled(conn, parked_seconds_ago=32 * HOUR)

    rc = kb_cli._cmd_schedule(_args(task_id=tid, now=True))
    assert rc == 0, capsys.readouterr().err
    assert "Wake set" in capsys.readouterr().out

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled" and task.next_eligible_at is not None
        assert "schedule_wake_set" in _kinds(conn, tid)

        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)
        assert res.woken_scheduled == [tid]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        # The wake stamp must not survive as a rate-limit cooldown.
        assert task.next_eligible_at is None
        kinds = _kinds(conn, tid)
        assert "unblocked" in kinds and "schedule_elapsed" in kinds
        assert res.unwoken_scheduled == []


def test_future_wake_holds_until_due(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _scheduled(conn)
        wake = int(time.time()) + 2 * HOUR
        assert kb.set_schedule_wake(conn, tid, wake_at=wake, actor="op")[0]
        assert kb.wake_due_scheduled(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.wake_due_scheduled(conn, now=wake) == [tid]
        assert kb.get_task(conn, tid).status == "ready"


def test_wake_regates_on_open_parents(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", parents=[parent], assignee="worker",
                               body="unblock_when: event:readback")
        assert kb.schedule_task(conn, child, reason="x")
        assert kb.set_schedule_wake(conn, child, wake_at=int(time.time()), actor="op")[0]
        assert kb.wake_due_scheduled(conn) == [child]
        assert kb.get_task(conn, child).status == "todo"


def test_cli_schedule_at_parks_and_stamps(kanban_home: Path, capsys) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="to park", assignee="worker")
    wake = int(time.time()) + 5 * HOUR
    rc = kb_cli._cmd_schedule(_args(task_id=tid, reason=["readback", "window"], at=str(wake)))
    assert rc == 0, capsys.readouterr().err
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled" and task.next_eligible_at == wake


def test_cli_schedule_at_iso(kanban_home: Path) -> None:
    assert kb_cli._parse_wake_at("2026-09-27T00:00:00+00:00") == 1790467200
    assert kb_cli._parse_wake_at("1790467200") == 1790467200
    with pytest.raises(ValueError):
        kb_cli._parse_wake_at("tomorrow-ish")


def test_wake_refused_on_non_scheduled(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="ready", assignee="worker")
        ok, err = kb.set_schedule_wake(conn, tid, wake_at=0, actor="op")
        assert not ok and "only applies to 'scheduled'" in err


def test_schedule_clears_stale_rate_limit_stamp(kanban_home: Path) -> None:
    """A past rate-limit ``next_eligible_at`` must not become a timed wake.

    Without the clear, an event-waiting card (no --at) is un-parked with a
    ``schedule_elapsed`` event on the very next dispatcher tick.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rate-limited earlier", assignee="worker",
                             body="unblock_when: event:readback")
        with kb.write_txn(conn):
            # What the rate-limited / infra-unavailable exit path leaves behind.
            conn.execute(
                "UPDATE tasks SET next_eligible_at=? WHERE id=?",
                (int(time.time()) - HOUR, tid),
            )
        assert kb.schedule_task(conn, tid, reason="wait for readback event")
        assert kb.get_task(conn, tid).next_eligible_at is None
        assert kb.wake_due_scheduled(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        assert "schedule_elapsed" not in _kinds(conn, tid)


def test_manual_unblock_clears_wake_stamp(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _scheduled(conn)
        assert kb.set_schedule_wake(
            conn, tid, wake_at=int(time.time()) + HOUR, actor="op",
        )[0]
        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.next_eligible_at is None


def test_operator_complete_closes_a_scheduled_card_in_one_step(kanban_home: Path) -> None:
    """t_63ebf58b: no unblock first, so no review state and no lifecycle-repair review_requested."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="parked review", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert kb.schedule_task(conn, tid, reason="readback", wake_at=int(time.time()) + HOUR)
        before = len(_kinds(conn, tid))
        # Parked from review: closes as the review approval, evidence optional (as from review).
        assert kb.complete_task(conn, tid, scheduled_ok=True)
        task = kb.get_task(conn, tid)
        assert task.status == "done" and task.next_eligible_at is None
        assert _kinds(conn, tid)[before:].count("review_requested") == 0
        assert "unblocked" not in _kinds(conn, tid)[before:]

        ready = _scheduled(conn, title="parked ready")
        with pytest.raises(kb.EmptyCompletionError):
            kb.complete_task(conn, ready, scheduled_ok=True)
        assert kb.get_task(conn, ready).status == "scheduled"
        # A worker run never owns a scheduled card: the run-bound close stays refused.
        assert not kb.complete_task(conn, ready, summary="x", expected_run_id=1, scheduled_ok=True)
        # Internal callers (no scheduled_ok) never end a park.
        assert not kb.complete_task(conn, ready, summary="x")
        assert "running/ready/blocked/review" in kb.explain_complete_refusal(conn, ready, expected_run_id=1)
        assert kb.complete_task(conn, ready, summary="event never came; closing", scheduled_ok=True)
        assert kb.get_task(conn, ready).status == "done"


def test_review_policy_fallback_does_not_close_a_scheduled_card(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prism 63a0700a7c19: request-review under review_policy=none must not end a park."""
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "none")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="parked", assignee="worker")
        assert kb.schedule_task(conn, tid, reason="readback", wake_at=int(time.time()) + HOUR)
        kb.request_review(conn, tid, summary="Tests passed")
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled" and task.next_eligible_at is not None


def test_cli_complete_closes_a_scheduled_card(kanban_home: Path, capsys) -> None:
    with kb.connect_closing() as conn:
        tid = _scheduled(conn)
    wrap = argparse.ArgumentParser(prog="wrap", add_help=False)
    parser = kb_cli.build_parser(wrap.add_subparsers(dest="_top"))
    rc = kb_cli.kanban_command(parser.parse_args(["complete", tid, "--summary", "event never came"]))
    assert rc == 0, capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"


def test_promote_refusal_names_the_scheduled_exit(kanban_home: Path) -> None:
    # ``force=`` was retired with upstream #106195 (a forced promotion only
    # reported a success the first claim reverted); the scheduled-exit hint
    # is what this test pins.
    with kb.connect_closing() as conn:
        tid = _scheduled(conn)
        ok, err = kb.promote_task(conn, tid, actor="op")
        assert not ok
        assert "unblock" in err and "schedule" in err and "--now" in err
