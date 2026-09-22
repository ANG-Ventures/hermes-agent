"""t_7863ffc6: a broken object store must name the repo, the lender and a remedy.

A scratch workspace accumulates disposable sibling clones that borrow objects
via `.git/objects/info/alternates` (`git clone --shared`/`--reference`; a plain
`git clone <local-path>` hardlinks instead and writes no alternates file at
all -- measured on git 2.53.0). When the lender is later pruned or reaped
the borrower becomes unreadable, `_snapshot`'s `git bundle` dies inside
pack-objects, and `_git(check=True)` collapses that into the constant
`survivor_unavailable: git inspection failed`. The card then cannot reach a
terminal state and the operator gets no repo, no cause and no remedy -- on
t_85cc093e round 11 that cost ~15 minutes of manual forensics.

Fail-closed is correct and must stay: these tests pin the MESSAGE, never a skip.
"""
import json
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


def borrowed_workspace(conn, tmp_path, *, publish=True, commit_local=False,
                       lender_outside=False, record_before_clone=False):
    """A workspace holding a `redprove` clone that BORROWS another repo's objects.

    Returns (task id, workspace, lender, borrower). The borrower is left intact;
    `prune_lender` below is what breaks it.

    Knobs, each pinning one reachable shape of the incident:
      publish             -- is the lender's HEAD on a durable remote at all
      commit_local        -- does the borrower COMMIT (ancestry depth >= 1) or
                             only stage; the two legs of `_object_store_broken`
                             do NOT both fire once a local commit exists
      lender_outside      -- is the lender a reapable sibling INSIDE the
                             workspace, or an external store
      record_before_clone -- is the borrower present at dispatch (so it lands in
                             `bases`) or created by a later round
    """
    tid = kb.create_task(conn, title="clone that borrows objects")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(parents=True, exist_ok=True)
    remote = tmp_path / f"{tid}.git"
    git(tmp_path, "init", "--bare", str(remote))

    cold = (tmp_path / f"{tid}-external-cold") if lender_outside else (ws / "cold")
    cold.mkdir()
    _init(cold)
    for i in range(5):
        (cold / f"f{i}.txt").write_text(f"line {i}\n")
        git(cold, "add", ".")
        git(cold, "commit", "-m", f"c{i}")
    git(cold, "remote", "add", "origin", str(remote))
    if publish:
        git(cold, "push", "origin", "HEAD:refs/heads/published")
    if record_before_clone:
        kb.set_workspace_path(conn, tid, ws)

    # `git clone --shared <local path>` records an alternates lender instead of
    # copying objects. This is the shape the incident had.
    git(ws, "clone", "--shared", str(cold), str(ws / "redprove"))
    redprove = ws / "redprove"
    _init(redprove)
    git(redprove, "remote", "set-url", "origin", str(remote))
    (redprove / "mutant.txt").write_text("deliberate red-prove mutant\n")
    git(redprove, "add", ".")
    if commit_local:
        git(redprove, "commit", "-m", "local work the worker committed")
    if not record_before_clone:
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


def test_a_repo_recorded_at_dispatch_is_told_the_MOVE_needs_a_survivor_flag(board, tmp_path):
    """B2: following the remedy literally must not dead-end on the SECOND gate.

    `preserve` refuses again with "recorded repository missing" when a repo that
    was in `bases` at dispatch is no longer present -- which is exactly what the
    instructed quarantine MOVE produces. Since nothing is auto-skipped, the
    instruction IS the product, so it has to name both steps. Measured on the
    live board: t_cc7c8c76 had both `cold` and `redprove` in `bases`.
    """
    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path)
    assert "redprove" in json.loads(
        board.execute("SELECT bases FROM task_workspace_survivors WHERE task_id = ?",
                      (tid,)).fetchone()[0]
    )
    prune_lender(cold)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    message = str(excinfo.value)
    assert "recorded at dispatch" in message
    assert "--survivor-pr" in message and "--survivor-ref" in message
    assert "recorded repository missing" in message

    # ...and the warning is TRUE: the bare MOVE really does hit that second gate.
    shutil.move(str(redprove), str(tmp_path / "quarantined-redprove"))
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    assert "recorded repository missing" in str(excinfo.value)


def test_a_repo_absent_at_dispatch_is_NOT_told_to_pair_the_move(board, tmp_path):
    """The other arm: a clone made by a LATER round is not in `bases`.

    Teeth for the test above -- a message that always mentioned the survivor
    flags would pass it. Here the plain MOVE is sufficient and the advice must
    not bolt on a step the operator does not need. This is the original
    incident's shape (t_85cc093e had bases={}), which is why the gap hides.
    """
    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path, record_before_clone=True)
    assert "redprove" not in json.loads(
        board.execute("SELECT bases FROM task_workspace_survivors WHERE task_id = ?",
                      (tid,)).fetchone()[0]
    )
    prune_lender(cold)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    message = str(excinfo.value)
    assert "MOVE" in message
    assert "recorded at dispatch" not in message
    assert "--survivor-pr" not in message

    # Following the remedy literally COMPLETES this arm; no second refusal.
    shutil.move(str(redprove), str(tmp_path / "quarantined-redprove"))
    assert kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})


def test_a_locally_committed_borrower_is_still_diagnosed(board, tmp_path):
    """C1: the `rev-list` leg is load-bearing once ancestry depth >= 1.

    The shipped test only STAGES. When the worker has COMMITTED locally, the new
    commit's own tree object exists in the borrower's store, so
    `cat-file -e HEAD^{tree}` succeeds and ONLY the reachability walk catches the
    missing borrowed parents -- while the real capture (`git bundle`) still dies.
    Dropping that leg would silently regress this common shape to the bare
    constant, so pin both the leg asymmetry and the resulting message.
    """
    import hermes_cli.kanban_survivor as survivor

    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path, commit_local=True)
    prune_lender(cold)

    def rc(*args):
        return subprocess.run(["git", "-C", str(redprove), *args],
                              capture_output=True).returncode

    # The asymmetry itself, measured rather than assumed.
    assert rc("cat-file", "-e", "HEAD^{tree}") == 0
    assert rc("rev-list", "--objects", "--no-object-names", "--max-count=1", "HEAD") != 0
    assert rc("bundle", "create", str(tmp_path / "c1.bundle"), "HEAD") != 0
    assert survivor._object_store_broken(redprove)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    message = str(excinfo.value)
    assert "git inspection failed" not in message
    assert "./redprove" in message and "broken object store" in message


def test_an_unpublished_local_commit_is_never_told_to_MOVE(board, tmp_path):
    """C2: the bar for MOVE is an EXACT advertised tip, not "some ref exists".

    The remote here DOES advertise `published` (the lender's tip), but the
    borrower's HEAD is a local commit that was never pushed. Matching any
    advertised ref instead of the exact tip would advise quarantining genuinely
    unpublished work -- the data-loss direction.
    """
    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path, commit_local=True)
    head = git(redprove, "rev-parse", "HEAD")
    prune_lender(cold)
    assert any(line for line in subprocess.run(
        ["git", "-C", str(redprove), "ls-remote", "--heads", "origin"],
        capture_output=True).stdout.decode().splitlines()), "remote must advertise something"

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    message = str(excinfo.value)
    assert head[:12] in message
    assert "not advertised on any durable remote" in message
    assert "do not delete" in message
    assert "MOVE" not in message


def test_an_external_lender_is_not_called_a_sibling_clone(board, tmp_path):
    """C3: an EXTERNAL lender is a different diagnosis from a reapable sibling.

    "sibling clone inside this workspace" tells the operator the store was
    always doomed; saying it about a lender outside the workspace would send
    them looking for a clone that is not there.
    """
    tid, ws, cold, redprove = borrowed_workspace(board, tmp_path, lender_outside=True)
    assert not cold.is_relative_to(ws)
    prune_lender(cold)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["mutant.txt"]})
    message = str(excinfo.value)
    assert "alternates" in message
    assert str(cold / ".git" / "objects") in message
    assert "external" in message
    assert "inside this workspace" not in message


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
