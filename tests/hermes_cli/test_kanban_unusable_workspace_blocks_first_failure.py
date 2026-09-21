"""A spawn failure that can NEVER succeed on retry must block on the FIRST try.

Incident (2026-09-20/21): ``~/dev/fleetreview-router`` was converted to a BARE
git repo at 21:32. Seven cards created 23:29-23:31 with
``workspace_kind='worktree'`` anchored there each hit
``_resolve_worktree_workspace`` -> ``ValueError: ... is not inside a git repo
and does not point at a git repo root``. That error is DETERMINISTIC — the repo
shape does not change between dispatcher ticks — yet each card burned the full
retry budget (3 spawns apiece, 21 total) before ``gave_up`` dumped it into
``blocked`` with an untyped block and no actionable reason.

The retry budget exists for TRANSIENT spawn failures (a busy host, a locked
index, a momentarily-missing mount). A malformed/unusable workspace anchor is a
``capability`` wall: no number of retries fixes it, only a human re-pointing the
card. So it must block on failure #1, with ``block_kind='capability'`` and a
reason that names the fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home, all_assignees_spawnable):
    with kb.connect() as c:
        yield c


def _bare_repo(tmp_path) -> str:
    """A real bare repo — the exact fleetreview-router shape."""
    import subprocess

    repo = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(repo)], check=True)
    return str(repo)


def _task_row(conn, task_id):
    return conn.execute(
        "SELECT status, consecutive_failures, block_kind, block_recurrences, "
        "last_failure_error "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _spawn_should_not_run(task, workspace_path, board=None):  # pragma: no cover
    raise AssertionError("spawn_fn must not be reached for an unusable workspace")


def test_bare_repo_worktree_blocks_capability_on_first_failure(conn, tmp_path):
    """One tick against a bare-repo anchor => blocked, kind=capability, 1 failure."""
    task_id = kb.create_task(
        conn,
        title="anchored on a bare repo",
        assignee="daedalus-opus",
        workspace_kind="worktree",
        workspace_path=_bare_repo(tmp_path),
    )

    result = kb.dispatch_once(conn, spawn_fn=_spawn_should_not_run)

    assert task_id in result.spawn_failed
    row = _task_row(conn, task_id)
    assert row["status"] == "blocked", (
        "an unusable workspace anchor must block immediately, not go back to ready "
        f"for 2 more doomed spawns (got status={row['status']!r})"
    )
    assert row["consecutive_failures"] == 1, (
        "the card must be blocked on failure #1 — the whole point is not burning "
        f"the retry budget (got {row['consecutive_failures']})"
    )
    assert row["block_kind"] == "capability", (
        "a deterministic workspace wall is a capability block (human must re-point "
        f"the card), not an untyped/transient one (got {row['block_kind']!r})"
    )
    assert task_id in result.auto_blocked


def test_capability_block_reason_names_the_fix(conn, tmp_path):
    """The blocked reason must tell the human what to DO, not just what broke."""
    task_id = kb.create_task(
        conn,
        title="anchored on a bare repo",
        assignee="daedalus-opus",
        workspace_kind="worktree",
        workspace_path=_bare_repo(tmp_path),
    )

    kb.dispatch_once(conn, spawn_fn=_spawn_should_not_run)

    row = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('blocked', 'gave_up') ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None, "the block must be recorded as an event"
    blob = (row["payload"] or "") + (
        _task_row(conn, task_id)["last_failure_error"] or ""
    )
    low = blob.lower()
    assert "workspace" in low
    assert "--workspace" in low or "re-create" in low or "scratch" in low, (
        "the reason must name the operator fix (re-point/re-create the workspace), "
        f"got: {blob[:400]!r}"
    )


def test_transient_spawn_failure_still_gets_its_retry_budget(conn, tmp_path):
    """NEGATIVE CONTROL: an ordinary spawn error must NOT be escalated.

    Without this, 'block on first failure' could be implemented by blocking on
    EVERY failure — which would strip the retry budget from genuinely flaky
    spawns (busy host, transient fork failure) that a retry does fix.
    """
    task_id = kb.create_task(
        conn,
        title="ordinary scratch card whose spawn flakes",
        assignee="daedalus-opus",
        workspace_kind="scratch",
    )

    def flaky_spawn(task, workspace_path, board=None):
        raise RuntimeError("fork: Resource temporarily unavailable")

    kb.dispatch_once(conn, spawn_fn=flaky_spawn)

    row = _task_row(conn, task_id)
    assert row["consecutive_failures"] == 1
    assert row["status"] == "ready", (
        "a transient spawn failure must go back to ready for its retry, not be "
        f"escalated to a capability block (got {row['status']!r})"
    )
    assert row["block_kind"] is None


# ---------------------------------------------------------------------------
# FleetReview 437b0de2 follow-ups. Each test below reproduces a finding that
# made the "block on failure #1" guarantee above only nominally true.
# ---------------------------------------------------------------------------


def _events(conn, task_id, kinds):
    marks = ",".join("?" * len(kinds))
    return [
        r["kind"] for r in conn.execute(
            f"SELECT kind FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
            "ORDER BY id",
            (task_id, *kinds),
        )
    ]


def test_capability_block_is_sticky_against_recompute_ready(conn, tmp_path):
    """P1: the block must SURVIVE the next tick's ``recompute_ready``.

    ``_record_task_failure`` emits ``gave_up``, which ``_has_sticky_block``
    deliberately does not treat as sticky (a breaker trip is meant to
    auto-recover once transient conditions clear). A capability wall is the
    opposite: nothing clears it but a human. Measured on 437b0de2, the card
    was promoted straight back to ``ready`` on the next tick with
    ``consecutive_failures`` still at 1 — so it re-spawned, re-failed, and
    burned the whole budget anyway. The block was cosmetic.
    """
    task_id = kb.create_task(
        conn,
        title="anchored on a bare repo",
        assignee="daedalus-opus",
        workspace_kind="worktree",
        workspace_path=_bare_repo(tmp_path),
    )

    kb.dispatch_once(conn, spawn_fn=_spawn_should_not_run)
    assert _task_row(conn, task_id)["status"] == "blocked"

    kb.recompute_ready(conn)

    assert _task_row(conn, task_id)["status"] == "blocked", (
        "a capability wall must stay blocked across ticks; auto-promoting it "
        "back to ready restores the exact retry burn this change removes"
    )


def test_capability_block_emits_a_sticky_blocked_event(conn, tmp_path):
    """P1 (mechanism): stickiness must come from a real ``blocked`` event.

    ``_has_sticky_block`` reads ``task_events``, not ``tasks.status`` — a
    status-only write is promoted on the next tick (the raw-SQL-park trap).
    """
    task_id = kb.create_task(
        conn,
        title="anchored on a bare repo",
        assignee="daedalus-opus",
        workspace_kind="worktree",
        workspace_path=_bare_repo(tmp_path),
    )

    kb.dispatch_once(conn, spawn_fn=_spawn_should_not_run)

    assert _events(conn, task_id, ("blocked", "unblocked"))[-1:] == ["blocked"], (
        "the capability block must emit a 'blocked' event so _has_sticky_block "
        "holds it; 'gave_up' alone is explicitly non-sticky"
    )


def test_capability_block_arms_the_unblock_loop_breaker(conn, tmp_path):
    """P1: a re-blocked capability wall must escalate, not spin forever.

    ``block_task`` counts ``block_recurrences`` so that unblock -> re-block for
    the same cause escalates to ``triage`` at ``BLOCK_RECURRENCE_LIMIT``. A
    capability block written directly by the breaker never touched that
    counter, so an operator who unblocks without fixing the anchor gets an
    unbounded unblock/re-block loop with no escalation.
    """
    task_id = kb.create_task(
        conn,
        title="anchored on a bare repo",
        assignee="daedalus-opus",
        workspace_kind="worktree",
        workspace_path=_bare_repo(tmp_path),
    )

    kb.dispatch_once(conn, spawn_fn=_spawn_should_not_run)
    assert _task_row(conn, task_id)["block_recurrences"] == 1, (
        "the first capability block must arm the recurrence counter"
    )

    # Operator unblocks without fixing the anchor; the card re-fails.
    kb.unblock_task(conn, task_id)
    kb.dispatch_once(conn, spawn_fn=_spawn_should_not_run)

    row = _task_row(conn, task_id)
    assert row["block_recurrences"] >= kb.BLOCK_RECURRENCE_LIMIT
    assert row["status"] == "triage", (
        "a second identical capability block must escalate to triage via the "
        f"unblock-loop breaker, not re-block silently (got {row['status']!r})"
    )


def test_missing_default_workdir_is_not_treated_as_permanent():
    """P1: 'has no default_workdir' is BOARD METADATA — it can change.

    The other two markers describe the shape of a path on disk, which no tick
    changes. A board's ``default_workdir`` is editable, so a card blocked on it
    can legitimately become dispatchable without anyone touching the card.
    Classifying it as a permanent capability wall blocks a recoverable card on
    failure #1 and denies it its retry budget.
    """
    exc = ValueError("board 'default' has no default_workdir set")
    assert kb._unusable_workspace_reason(exc) is None


def test_operator_fix_survives_the_500_char_truncation(conn, tmp_path):
    """P2: the actionable half of the reason must not be sliced off.

    ``_record_task_failure`` stores ``error[:500]``. The operator fix was
    APPENDED after the raw exception text, so a long workspace path pushes the
    entire 'here is what to do' half past the cut. Measured on 437b0de2 with a
    deep path: stored length exactly 500, ending mid-path, with zero fix text.
    """
    deep = tmp_path / ("d" * 180) / ("e" * 180)
    deep.mkdir(parents=True)
    repo = deep / "bare.git"
    import subprocess

    subprocess.run(["git", "init", "--bare", "-q", str(repo)], check=True)

    task_id = kb.create_task(
        conn,
        title="anchored on a bare repo behind a very long path",
        assignee="daedalus-opus",
        workspace_kind="worktree",
        workspace_path=str(repo),
    )

    kb.dispatch_once(conn, spawn_fn=_spawn_should_not_run)

    stored = _task_row(conn, task_id)["last_failure_error"]
    assert len(stored) <= 500
    assert "--workspace scratch" in stored, (
        "the operator fix must lead so truncation eats the replaceable path "
        f"detail, not the instructions (got {stored!r})"
    )
