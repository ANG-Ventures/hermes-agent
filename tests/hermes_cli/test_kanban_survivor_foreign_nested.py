"""A shared ``dir`` workspace must not HOLD on ANOTHER card's nested repo.

Card t_10e7c7ad. Every card whose workspace is ``dir:~/.hermes`` was refused
at completion with ``survivor_unavailable: nested repository requires separate
recovery (.worktrees/argus/t_0c1ebbae-r2)`` -- an argus review worktree that
belongs to a different card. Five blameless blocks on 2026-09-25.

Contract pinned here:
  * a nested repo with a NAMED foreign owner (another card's id, another
    card's workspace_path, another profile's ``.worktrees/<profile>`` area) or
    one that PREDATES the card is skipped on the completion pass of a ``dir``
    workspace;
  * a nested repo the card itself created, one with no owner evidence, or one
    holding the card's own ``changed_files`` still fails closed;
  * the reclamation pass (``cleanup=True``) and non-``dir`` workspaces are
    unchanged.

Mutation check: make ``_ForeignNested._owner`` return None -> every
``*_is_skipped`` test goes red; make it always return an owner -> the
``*_still_refuses`` / ``*_is_not_foreign`` tests go red.
"""
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as survivor

FOREIGN_ID = "t_0c1ebbae"


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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def home(tmp_path):
    """A shared home repo; the card's own work is a dirty file in it."""
    ws = _init(tmp_path / "home")
    (ws / "code.py").write_text("value = 2\n")
    return ws


def _card(conn, ws, *, kind="dir", assignee="daedalus-opus", created_at=None):
    tid = kb.create_task(conn, title="dir card", assignee=assignee,
                         workspace_kind=kind, workspace_path=str(ws))
    # Pin the card's birth well clear of the fixture's repositories so the
    # predates-the-card rule is deterministic. Default: an hour in the PAST, so
    # every fixture repo counts as possibly the card's own and only the
    # name/owner rules can skip it. Predates tests pass a future time.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?",
                     (created_at if created_at is not None else int(time.time()) - 3600, tid))
    return tid


def _complete(conn, tid, ws, **kw):
    return survivor.preserve(conn, tid, {"changed_files": ["code.py"]}, workspace=ws, **kw)


def _captured_repositories(result):
    import json
    manifest = json.loads(Path(result["sidecar"]).read_text())
    return {entry["repository"] for entry in manifest.get("repositories", [])} | {
        b["repository"] for b in result.get("bundles") or ()}


# --- foreign nested repos are skipped ----------------------------------------


def test_foreign_argus_review_worktree_is_skipped(board, home):
    """The reported shape: a linked worktree named for another card."""
    git(home, "worktree", "add", str(home / ".worktrees" / "argus" / f"{FOREIGN_ID}-r2"), "-b", "review")
    assert survivor._registered_nested(home), "fixture: git must register the worktree"
    tid = _card(board, home)

    result = _complete(board, tid, home)

    assert result is not None and result["kind"] in {"bundle", "patch"}
    assert _captured_repositories(result) == {"."}, result
    _bases, held, _prev = survivor._state(board, tid)
    assert held is None


def test_foreign_profile_area_clone_is_skipped(board, home):
    """A hand clone in another profile's ``.worktrees/<profile>`` area."""
    kb.create_task(board, title="an argus card", assignee="argus")
    _init(home / ".worktrees" / "argus" / "t1a31-base")
    tid = _card(board, home)

    result = _complete(board, tid, home)
    assert _captured_repositories(result) == {"."}, result


def test_another_cards_workspace_path_is_skipped(board, home):
    """No id in the name; the board itself records the owner."""
    other = _init(home / "kanban" / "workspaces" / "scratchy")
    kb.create_task(board, title="owner", workspace_kind="dir", workspace_path=str(other))
    tid = _card(board, home)

    result = _complete(board, tid, home)
    assert _captured_repositories(result) == {"."}, result


# --- own / unowned nested repos still fail closed ----------------------------


def test_nested_worktree_the_card_created_still_refuses(board, home):
    tid = _card(board, home)
    git(home, "worktree", "add", str(home / ".worktrees" / "daedalus-opus" / tid), "-b", "mine")

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, home)
    assert "nested repository requires separate recovery" in str(caught.value)
    assert tid in str(caught.value)


def test_unowned_hand_clone_still_refuses(board, home):
    """Made after the card, no named owner: it may be the card's own clone."""
    tid = _card(board, home)
    _init(home / "cloned-by-hand")

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, home)
    assert "nested repository requires separate recovery" in str(caught.value)


def test_own_profile_area_is_not_foreign(board, home):
    """The card's own assignee's area is never somebody else's."""
    kb.create_task(board, title="an argus card", assignee="argus")
    tid = _card(board, home)
    _init(home / ".worktrees" / "daedalus-opus" / "scratch-clone")

    with pytest.raises(survivor.SurvivorUnavailable):
        _complete(board, tid, home)


def test_reclamation_pass_still_refuses_on_foreign_nested(board, home):
    """``cleanup=True`` may delete; it must still see every byte."""
    git(home, "worktree", "add", str(home / ".worktrees" / "argus" / f"{FOREIGN_ID}-r2"), "-b", "review")
    tid = _card(board, home)

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, home, cleanup=True)
    assert "nested repository requires separate recovery" in str(caught.value)


def test_worktree_workspace_still_refuses_on_foreign_nested(board, home):
    """Only ``dir`` workspaces are shared; other kinds are unchanged."""
    git(home, "worktree", "add", str(home / ".worktrees" / "argus" / f"{FOREIGN_ID}-r2"), "-b", "review")
    tid = _card(board, home, kind="worktree")

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, home)
    assert "nested repository requires separate recovery" in str(caught.value)


def test_repository_predating_the_card_is_skipped(board, home):
    """No name evidence, but it existed before the card: the card did not make it."""
    _init(home / "wt" / "land-retention")
    tid = _card(board, home, created_at=int(time.time()) + 3600)

    result = _complete(board, tid, home)
    assert _captured_repositories(result) == {"."}, result


def test_predating_repo_holding_claimed_files_still_refuses(board, home):
    """The card claimed work inside it: its own changed paths are never skipped."""
    _init(home / "skills-shared")
    tid = _card(board, home, created_at=int(time.time()) + 3600)

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        survivor.preserve(board, tid, {"changed_files": ["code.py", "skills-shared/code.py"]},
                          workspace=home)
    assert "nested repository requires separate recovery" in str(caught.value)
