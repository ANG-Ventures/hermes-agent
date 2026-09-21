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
        "SELECT status, consecutive_failures, block_kind, last_failure_error "
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
