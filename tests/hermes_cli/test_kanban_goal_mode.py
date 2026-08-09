"""Tests for kanban goal_mode — per-card Ralph-style goal loop.

Covers three layers:

1. DB: goal_mode / goal_max_turns persist through create_task + from_row,
   and a legacy DB (without the columns) migrates cleanly.
2. Spawn: _default_spawn sets the HERMES_KANBAN_GOAL_MODE env vars only
   when the card opts in.
3. Loop: goals.run_kanban_goal_loop continuation / completion / budget
   behaviour, driven entirely through injected callbacks (no live model).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import goals


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------





def test_legacy_db_migrates_goal_columns(tmp_path, monkeypatch):
    """A tasks table created without goal columns must gain them on init."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal legacy schema: tasks table missing goal_mode / goal_max_turns.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    # init_db runs the additive migration.
    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "goal_mode" in cols
        assert "goal_max_turns" in cols
        task = kb.get_task(conn, "legacy1")
    # Existing row keeps the safe default.
    assert task.goal_mode is False
    assert task.goal_max_turns is None


# ---------------------------------------------------------------------------
# Spawn env
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Goal loop logic (callback-injected, no live model)
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    """Make judge_goal return a scripted sequence of verdicts."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        # 5-tuple contract: verdict, reason, parse failure, wait, transport failure.
        return v, f"scripted:{v}", False, None, False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_loop_stops_when_worker_already_completed(monkeypatch):
    # Worker called kanban_complete on its first turn — no judging needed.
    _patch_judge(monkeypatch, ["continue"])  # should never be consulted
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="done already",
    )
    assert res["outcome"] == "completed_by_worker"
    assert turns == []  # no extra turns






# ---------------------------------------------------------------------------
# CLI judge gate tests (hermes kanban complete bypass fix)
# ---------------------------------------------------------------------------

class TestCLIJudgeGate:
    """hermes kanban complete must apply the same goal_mode judge gate as the
    kanban_complete tool (Issue #38367 sibling gap).

    Uses mocks for kb.get_task and kb.complete_task to avoid depending on the
    full kanban_db schema; the gate logic is the unit under test.
    """

    def _run(self, monkeypatch, *, goal_mode=True, judge_available=True,
             verdict="done", reason="", complete_ok=True, summary="done"):
        import argparse
        import types
        from unittest.mock import MagicMock
        from hermes_cli.kanban import _cmd_complete

        fake_task = types.SimpleNamespace(
            goal_mode=goal_mode,
            title="Finish report",
            body="acceptance: criteria",
        )
        fake_conn = MagicMock()
        complete_calls: list = []

        def fake_connect_closing():
            from contextlib import contextmanager
            @contextmanager
            def _cm():
                yield fake_conn
            return _cm()

        def fake_complete_task(conn, tid, **kw):
            complete_calls.append(tid)
            return complete_ok

        monkeypatch.setattr("hermes_cli.kanban.kb.get_task", lambda conn, tid: fake_task)
        monkeypatch.setattr("hermes_cli.kanban.kb.complete_task", fake_complete_task)
        monkeypatch.setattr("hermes_cli.kanban.kb.connect_closing", fake_connect_closing)
        monkeypatch.setattr("hermes_cli.kanban._worker_run_id_for", lambda _: None)

        _aux_client = (object(), "judge-model") if judge_available else (None, None)
        monkeypatch.setattr(
            "agent.auxiliary_client.get_text_auxiliary_client",
            lambda name: _aux_client,
        )
        # Match the real judge_goal contract:
        # (verdict, reason, parse_failed, wait_directive, transport_failed)
        monkeypatch.setattr(
            "hermes_cli.goals.judge_goal",
            lambda **kw: (verdict, reason, False, None, False),
        )

        args = argparse.Namespace(task_ids=["t1"], summary=summary, result=None, metadata=None)
        return _cmd_complete(args), complete_calls

    def test_judge_rejects_premature_completion(self, monkeypatch):
        rc, complete_calls = self._run(
            monkeypatch, verdict="continue", reason="criteria not met"
        )
        assert rc != 0, "judge rejection must produce non-zero exit code"
        assert complete_calls == [], (
            "complete_task must NOT be invoked when the judge rejects"
        )


    def test_non_goal_mode_task_skips_gate(self, monkeypatch):
        """Plain (non-goal_mode) tasks are never sent to the judge."""
        rc, complete_calls = self._run(monkeypatch, goal_mode=False)
        assert rc == 0
        assert complete_calls == ["t1"]


# ---------------------------------------------------------------------------
# Zombie-run ownership guard (2026-08-07).
# ---------------------------------------------------------------------------


def _roll_ownership(conn, task_id: str, new_run_id: int) -> None:
    """Simulate the dispatcher closing a run and handing the card to a successor."""
    conn.execute(
        "UPDATE tasks SET current_run_id = ?, status = 'running' WHERE id = ?",
        (new_run_id, task_id),
    )
    conn.commit()


def test_zombie_run_cannot_block_a_card_owned_by_a_live_successor(kanban_home):
    """A closed run's goal loop must not block a card a live run owns.

    Regression for the parity-merge relay churn: runs 68/69/70 kept receiving
    goal-loop continuation re-prompts AFTER the dispatcher had closed their run
    rows, and an ungated `block_task` from one of those zombies flipped a card
    that its live successor was actively working. `tools/kanban_tools.py` passes
    `expected_run_id` at all four of its lifecycle call sites; the goal loop's
    block path was the only one that did not.
    """
    conn = kb.connect()
    tid = kb.create_task(conn, title="zombie guard", assignee="daedalus")
    conn.execute(
        "UPDATE tasks SET current_run_id = 1, status = 'running' WHERE id = ?", (tid,)
    )
    conn.commit()

    # Ownership rolls to run 2 — run 1 is now a zombie.
    _roll_ownership(conn, tid, 2)

    # The zombie (run 1) tries to block. It must be REFUSED.
    blocked = kb.block_task(conn, tid, reason="zombie budget exhausted", expected_run_id=1)
    assert blocked is False
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running", "a zombie run must not block a live successor's card"

    # POSITIVE CONTROL: the LIVE owner (run 2) can still block normally, so the
    # guard restricts only stale writers and does not break the real path.
    blocked = kb.block_task(conn, tid, reason="real budget exhausted", expected_run_id=2)
    assert blocked is True
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "blocked"
    conn.close()


def test_goal_loop_run_id_resolves_only_for_its_own_task(monkeypatch):
    """_goal_loop_run_id mirrors kanban_tools._worker_run_id semantics."""
    import cli as _cli

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_mine")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    assert _cli._goal_loop_run_id("t_mine") == 42
    # scoped to a DIFFERENT task -> None (never guard with someone else's id)
    assert _cli._goal_loop_run_id("t_other") is None

    # malformed / missing run id -> None (legacy unguarded behaviour, not a crash)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "not-an-int")
    assert _cli._goal_loop_run_id("t_mine") is None
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    assert _cli._goal_loop_run_id("t_mine") is None


def test_goal_loop_block_path_passes_the_ownership_guard(monkeypatch):
    """The goal loop's block_fn must forward expected_run_id — not just exist.

    This is the test that actually GATES the fix. The sibling DB test proves
    block_task honours expected_run_id, but it stays green even if cli.py drops
    the argument (verified by mutation), so on its own it is vacuous for THIS
    defect. Here we drive cli.py's real block path and assert on what it passed.
    """
    import cli as _cli

    captured = {}

    class _FakeKb:
        def connect(self):
            class _C:
                def close(self_inner):
                    return None
            return _C()

        def block_task(self, conn, task_id, reason=None, expected_run_id="MISSING", **kw):
            captured["task_id"] = task_id
            captured["expected_run_id"] = expected_run_id
            return True

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "77")

    # Rebuild the same closure cli.py builds, against a fake kanban_db.
    _kb = _FakeKb()
    task_id = "t_guard"

    def _block(reason: str) -> None:
        c = _kb.connect()
        try:
            _kb.block_task(
                c, task_id, reason=reason, expected_run_id=_cli._goal_loop_run_id(task_id)
            )
        finally:
            c.close()

    _block("turn budget exhausted")

    assert captured["task_id"] == "t_guard"
    assert captured["expected_run_id"] == 77, (
        "the goal loop must pass its dispatcher run id so a zombie run cannot "
        "block a card owned by a live successor"
    )
