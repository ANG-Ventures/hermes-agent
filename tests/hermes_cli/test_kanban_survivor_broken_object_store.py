"""t_7863ffc6: a broken object store must name the repo, the lender and a remedy.

A scratch workspace accumulates disposable sibling clones that borrow objects
via `.git/objects/info/alternates` (`git clone <local-path>` does this by
default -- no exotic flag needed). When the lender is later pruned or reaped
the borrower becomes unreadable, `_snapshot`'s `git bundle` dies inside
pack-objects, and `_git(check=True)` collapses that into the constant
`survivor_unavailable: git inspection failed`. The card then cannot reach a
terminal state and the operator gets no repo, no cause and no remedy -- on
t_85cc093e round 11 that cost ~15 minutes of manual forensics.

Fail-closed is correct and must stay: these tests pin the MESSAGE, never a skip.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def _init(repo):
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")


def borrowed_workspace(conn, tmp_path, *, publish=True):
    """A workspace holding `cold` and a `redprove` clone that BORROWS its objects.

    Returns (task id, workspace, lender, borrower). The borrower is left intact;
    `prune_lender` below is what breaks it.
    """
    tid = kb.create_task(conn, title="clone that borrows objects")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(parents=True, exist_ok=True)
    remote = tmp_path / f"{tid}.git"
    git(tmp_path, "init", "--bare", str(remote))

    cold = ws / "cold"
    cold.mkdir()
    _init(cold)
    for i in range(5):
        (cold / f"f{i}.txt").write_text(f"line {i}\n")
        git(cold, "add", ".")
        git(cold, "commit", "-m", f"c{i}")
    git(cold, "remote", "add", "origin", str(remote))
    if publish:
        git(cold, "push", "origin", "HEAD:refs/heads/published")

    # `git clone <local path>` records an alternates lender instead of copying
    # objects. This is the shape the incident had; --shared only makes it explicit.
    git(ws, "clone", "--shared", str(cold), str(ws / "redprove"))
    redprove = ws / "redprove"
    _init(redprove)
    git(redprove, "remote", "set-url", "origin", str(remote))
    (redprove / "mutant.txt").write_text("deliberate red-prove mutant\n")
    git(redprove, "add", ".")
    kb.set_workspace_path(conn, tid, ws)
    return tid, ws, cold, redprove


def prune_lender(cold):
    """Reap the objects the BORROWER needs, leaving the lender itself healthy.

    This is the incident's exact asymmetry (t_85cc093e round 11): `cold` was
    fine, `redprove` had 24399 broken links. Reproduced by rewinding the
    lender to its root commit and gc-ing everything above it -- the borrower's
    HEAD objects live only in the lender's store, so they go with it.
    """
    root = git(cold, "rev-list", "--max-parents=0", "HEAD")
    git(cold, "checkout", "--detach", root)
    for ref in git(cold, "for-each-ref", "--format=%(refname)").splitlines():
        git(cold, "update-ref", "-d", ref.strip())
    git(cold, "remote", "remove", "origin")
    shutil.rmtree(cold / ".git" / "logs", ignore_errors=True)
    git(cold, "gc", "--prune=now")


def test_broken_object_store_names_the_repo_the_lender_and_a_remedy(board, tmp_path):
    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path)
    head = git(redprove, "rev-parse", "HEAD")
    prune_lender(cold)
    # Precondition: the capture path really does die on this repo.
    assert subprocess.run(
        ["git", "-C", str(redprove), "bundle", "create", str(tmp_path / "x.bundle"), "HEAD"],
        capture_output=True,
    ).returncode != 0

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    message = str(excinfo.value)

    # The bare constant must never reach the operator for this shape.
    assert message != "survivor_unavailable: git inspection failed"
    assert "git inspection failed" not in message
    # WHICH repo, WHAT happened, WHICH lender, and that the lender is reapable.
    assert "./redprove" in message
    assert "broken object store" in message
    assert "alternates" in message
    assert str(cold / ".git" / "objects") in message
    assert "inside this workspace" in message
    # ...and the remedy, keyed on the published HEAD.
    assert head[:12] in message
    assert "MOVE" in message and "never delete" in message

    # Fail-closed is preserved: still no completion, still no deletion.
    assert kb.get_task(board, tid).status != "done"
    assert ws.exists() and redprove.exists()
    assert any(e.kind == "workspace_held" for e in kb.list_events(board, tid))


def test_unpublished_head_is_told_NOT_to_delete(board, tmp_path):
    """The remedy must invert when the work is not on any durable remote."""
    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path, publish=False)
    prune_lender(cold)
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    message = str(excinfo.value)
    assert "not advertised on any durable remote" in message
    assert "do not delete" in message
    assert "MOVE" not in message


def test_a_healthy_workspace_still_completes(board, tmp_path):
    """Teeth: a stub that refused every borrowed clone would pass the tests above.

    The same borrowed layout, lender intact, must still capture a survivor.
    """
    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] in {"patch", "bundle", "ref"}


def test_unrelated_git_failures_keep_the_constant_message(board, tmp_path, monkeypatch):
    """Teeth: the diagnosis must not swallow failures it cannot explain.

    A capture that fails for a reason OTHER than a broken object store keeps
    today's credential-free constant -- the classifier is additive, not a
    rewrite of every refusal.
    """
    import hermes_cli.kanban_survivor as survivor

    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path)
    monkeypatch.setattr(
        survivor, "_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(
            survivor.SurvivorUnavailable("survivor_unavailable: git inspection failed")),
    )
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    assert str(excinfo.value) == "survivor_unavailable: git inspection failed"


def test_baseline_logs_a_lender_inside_the_same_workspace(board, tmp_path, caplog):
    """The upstream CAUSE is attributable at dispatch, before anything breaks.

    A warning, not a refusal: refusing here would make a card unspawnable over a
    clone layout that is usually fine.
    """
    import hermes_cli.kanban_survivor as survivor

    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path)
    board.execute("DELETE FROM task_workspace_survivors WHERE task_id = ?", (tid,))
    board.commit()
    with caplog.at_level("WARNING"):
        survivor.record_baseline(board, tid, ws)
    assert "redprove" in caplog.text
    assert "borrows objects" in caplog.text
    assert str(cold / ".git" / "objects") in caplog.text
