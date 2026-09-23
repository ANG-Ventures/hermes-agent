"""Survivor enumeration must be PRUNED and BOUNDED, never an unbounded walk.

Card t_6c46905a. `kanban_complete` on any card whose workspace is
`dir@/Users/alexgierczyk/.hermes` hung with NO output and NO error until killed
(200 s+ across three attempts; the tool died at its 420 s ceiling having written
no terminal state at all). `faulthandler` caught it inside `pathlib.is_symlink`
under the `os.walk` in `_repos`.

MEASURED on that tree before the fix:

    unpruned walk        850,853 directories / 227.5 s  (idle disk, completed)
                         853,413 `is_symlink()` stat calls
    at 60 s              279,656 directories, nowhere near done
    `git worktree list`  1.2 s, and proves a nested repo is present
    `git ls-files`       0.05 s for the gitlink entries

The card also prescribed two remedies that MEASUREMENT REJECTS, and these tests
pin the rejection so a future reader does not re-adopt them:

  * Pruning the big subtrees it named (`.worktrees`, `kanban`, `var`, `wt`,
    `plans`, `skills-shared`, `sessions`, `runs`, `logs`, `backups`) drops
    **691 of the 832** repository keys recorded in `bases` on the live board --
    it prunes away the repositories the walk exists to find. `_DERIVED_DIRS`
    drops 0 of them. `test_derived_prune_never_hides_a_repository` is the gate.
  * REPLACING the walk with `git worktree list` + gitlinks recovers only
    **47 of the 813** repositories recorded for card t_f5ebd9db, because a repo
    a worker `git clone`d by hand is in neither registry. So the git-native path
    is used only as a POSITIVE short-circuit.
    `test_hand_made_clone_is_still_found` is the gate.

Mutation checks (each run against this file):
  * restore the unbounded walk -> `test_budget_refuses_instead_of_hanging` red
  * drop the `_registered_nested` short-circuit -> `test_home_shaped_workspace_refuses_fast` red
  * add `.worktrees` to `_DERIVED_DIRS` -> `test_derived_prune_never_hides_a_repository` red
"""
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as survivor


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


def _init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "code.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def _junk(root, count, *, name):
    """`count` directories buried under a single `name` child of `root`."""
    base = root / name
    for i in range(count):
        (base / f"{i // 100}" / f"d{i}").mkdir(parents=True, exist_ok=True)
    return base


# --- (a) an excluded subtree costs nothing --------------------------------


def test_derived_subtree_is_not_walked(tmp_path):
    """50k directories under a derived name must not be visited at all.

    The wall clock is asserted loosely (< 5 s) only as a smoke bound; the
    DETERMINISTIC assertion is the visit count, which cannot flake on a loaded
    runner. Unpruned, this tree alone is >50,000 entries and would blow the
    default budget -- so an unpruned implementation raises here rather than
    merely running slowly.
    """
    ws = _init(tmp_path / "ws")
    _junk(ws, 50_000, name="node_modules")

    started = time.monotonic()
    visited = [path for path, _is_repo in survivor._walk(ws)]
    elapsed = time.monotonic() - started

    assert not any("node_modules" in p.parts for p in visited), (
        "the walk descended into a derived directory it must prune"
    )
    assert len(visited) < 50, f"expected a handful of real dirs, visited {len(visited)}"
    assert elapsed < 5, f"pruned walk of a 50k-entry derived tree took {elapsed:.1f}s"


def test_derived_prune_never_hides_a_repository():
    """The prune set may only name DERIVED dirs, never big ones.

    This is the coverage gate on `_DERIVED_DIRS`. The names below are the ones
    t_6c46905a proposed pruning; on the live board they hold 691 of the 832
    recorded repository keys (`kanban/workspaces/*` alone holds 422), so
    pruning any of them silently converts a fail-closed HOLD into a delete for
    real unpushed work.
    """
    forbidden = {
        ".worktrees", "worktrees", "wt", "kanban", "var", "plans", "skills-shared",
        "profiles", "sessions", "runs", "logs", "backups", "projects", "deploy",
        "hermes-agent", "greenhouse",
    }
    overlap = forbidden & set(survivor._DERIVED_DIRS)
    assert not overlap, (
        f"{sorted(overlap)} can hold real repositories; pruning them loses survivors. "
        "Re-measure against every `bases` row before adding a name here."
    )


def test_derived_prune_does_not_hide_a_repo_named_like_one(tmp_path):
    """A pruned name is not consulted, so a repo INSIDE one is invisible.

    Documents the accepted cost of the prune rather than asserting a bug: a
    repository a worker put inside `node_modules` is not found. That is the
    price of bounding the walk, and it is why the set stays limited to trees a
    tool can regenerate.
    """
    ws = _init(tmp_path / "ws")
    _init(ws / "node_modules" / "buried")
    assert ws.resolve() in [p.resolve() for p in survivor._repos(ws)]
    assert (ws / "node_modules" / "buried") not in survivor._repos(ws)


# --- (b) the budget fires with a NAMED error ------------------------------


def test_budget_refuses_instead_of_hanging(tmp_path, monkeypatch):
    """Over budget must raise a legible refusal, never run to completion.

    The refusal text is what lands in `held_reason` and is replayed to a
    worker, so it has to name the count and the path -- the two facts that tell
    an operator which tree to point the card at instead. A gate that cannot
    finish must SAY so.
    """
    ws = _init(tmp_path / "ws")
    monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", "64")
    _junk(ws, 400, name="src")

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        survivor._repos(ws)

    message = str(caught.value)
    assert "survivor_unavailable:" in message
    assert "64" in message, f"the budget is not named: {message}"
    assert str(ws) in message, f"the path is not named: {message}"
    assert "HERMES_KANBAN_SURVIVOR_WALK_BUDGET" in message, (
        f"the refusal must name its own override: {message}"
    )


def test_budget_also_bounds_the_loose_file_scan(tmp_path, monkeypatch):
    """`_loose_files` had the same unbounded shape and needs the same bound."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _junk(ws, 400, name="notes")
    monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", "64")

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        survivor._loose_files(ws, [])
    assert "loose-file scan" in str(caught.value)


def test_budget_env_override_is_honoured_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", "1234")
    assert survivor._walk_budget() == 1234
    for bad in ("0", "-5", "abc", ""):
        monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", bad)
        assert survivor._walk_budget() == survivor.DEFAULT_WALK_BUDGET_ENTRIES


def test_baseline_over_budget_does_not_make_the_card_unspawnable(board, tmp_path, monkeypatch):
    """`record_baseline` runs BEFORE dispatch; refusing there kills the card.

    Same failure mode the EACCES skip exists to prevent. An empty `bases` is
    the honest record for a tree we could not enumerate, and it costs no
    survivor: `preserve` re-enumerates under the same budget at completion,
    where there is a run to HOLD.
    """
    ws = _init(tmp_path / "ws")
    _junk(ws, 400, name="src")
    monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", "64")

    tid = kb.create_task(board, title="over budget at dispatch")
    survivor.record_baseline(board, tid, ws)  # must not raise

    bases, _held, _survivor = survivor._state(board, tid)
    assert bases == {}, f"an unenumerable tree must record no bases, got {bases}"


# --- (c) no regression on the thing the walk exists for -------------------


def test_nested_worktree_is_still_found(tmp_path):
    """The whole point of the walk: a linked worktree inside must be found."""
    ws = _init(tmp_path / "ws")
    git(ws, "worktree", "add", str(ws / "nested"), "-b", "side")
    found = {p.resolve() for p in survivor._repos(ws)}
    assert (ws / "nested").resolve() in found


def test_hand_made_clone_is_still_found(tmp_path):
    """A repo in NEITHER git registry must still be found by the walk.

    This is why the git-native path cannot REPLACE the walk. Measured on the
    live board, `worktree list` + gitlinks account for only 47 of the 813
    repositories recorded for t_f5ebd9db; the rest are clones workers made by
    hand, exactly like this one.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    _init(ws / "cloned-by-hand")
    found = {p.resolve() for p in survivor._repos(ws)}
    assert (ws / "cloned-by-hand").resolve() in found
    assert not survivor._registered_nested(ws), (
        "a hand-made clone must be invisible to the git registries -- "
        "that is the premise this test protects"
    )


def test_registered_nested_finds_a_worktree_without_walking(tmp_path, monkeypatch):
    """The fast path must answer from git metadata, with NO directory scan."""
    ws = _init(tmp_path / "ws")
    git(ws, "worktree", "add", str(ws / "nested"), "-b", "side")

    def forbidden(_path):
        raise AssertionError("_registered_nested must not walk the filesystem")

    monkeypatch.setattr(survivor, "_subdirs", forbidden)
    registered = survivor._registered_nested(ws)
    assert (ws / "nested").resolve() in {p.resolve() for p in registered}


def test_home_shaped_workspace_refuses_fast(board, tmp_path, monkeypatch):
    """The reported bug, end to end through `preserve`.

    A workspace that is itself a repo AND contains a nested one always reaches
    "nested repository requires separate recovery" -- so the 227 s walk was
    deriving a foregone verdict. The budget is set below the junk count here:
    an implementation that walks first blows the budget and produces the WRONG
    refusal, while the fixed one answers from git metadata and never looks.
    """
    ws = _init(tmp_path / "home")
    git(ws, "worktree", "add", str(ws / "nested"), "-b", "side")
    _junk(ws, 400, name="var")
    monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", "64")

    tid = kb.create_task(board, title="home as workspace")
    kb.set_workspace_path(board, tid, ws)

    started = time.monotonic()
    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        survivor.preserve(board, tid, {"changed_files": ["code.py"]}, workspace=ws)
    elapsed = time.monotonic() - started

    message = str(caught.value)
    assert "nested repository requires separate recovery" in message, message
    assert "nested" in message, f"the refusal must name the offender: {message}"
    assert "budget" not in message, (
        f"an implementation that walks before asking git produced: {message}"
    )
    assert elapsed < 10, f"refusal took {elapsed:.1f}s; it must not walk first"


def test_loose_files_still_reports_a_symlink(tmp_path):
    """A symlink is evidence no ref vouches for; the scandir form must see it.

    `is_dir(follow_symlinks=False)` is False for a symlink-to-file but a
    symlink-to-DIRECTORY would be classified as a directory and skipped, so the
    symlink test has to precede the directory branch.
    """
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert survivor._loose_files(ws, []) is True


def test_loose_files_is_false_for_a_bare_tree_of_dirs(tmp_path):
    ws = tmp_path / "ws"
    (ws / "a" / "b").mkdir(parents=True)
    assert survivor._loose_files(ws, []) is False


def test_loose_files_ignores_a_derived_tree(tmp_path):
    """A `node_modules` tree is not evidence a survivor must vouch for."""
    ws = tmp_path / "ws"
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "index.js").write_text("x\n")
    assert survivor._loose_files(ws, []) is False


def test_unreadable_directory_is_skipped_not_fatal(tmp_path):
    """Preserves the 2026-09-20 fix: EACCES must not take down enumeration."""
    ws = _init(tmp_path / "ws")
    locked = ws / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        found = {p.resolve() for p in survivor._repos(ws)}
    finally:
        locked.chmod(0o755)
    assert ws.resolve() in found
