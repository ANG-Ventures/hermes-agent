"""A nested repository does not veto a completion whose explicit survivor LANDED.

Card t_3d376252. t_3ecbb73d's scratch workspace held ``repo/`` with a linked
worktree ``repo/mainwt`` inside it. Its PR (hermes-home#5702) was merged, yet
``kanban complete --survivor-pr`` refused with ``nested repository requires
separate recovery`` on every attempt, so a done card could not be closed.

The refusal still guards what it was built for: a completion with no survivor,
or with a survivor that is published but not landed (an OPEN PR), and the
cleanup pass that would delete the nested repository's bytes.

Mutation check: make ``_landed_claims`` return False ->
``test_merged_survivor_completes_beside_nested_repos`` goes red.
"""
import json
import subprocess
from pathlib import Path

import pytest

from tests.hermes_cli._survivor_gh_fake import rest_pr

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw

HEAD = "a1" * 20
MERGE = "b2" * 20
PR = "example/project#68"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "protocol.file.allow=always", *args],
        stdin=subprocess.DEVNULL, capture_output=True, check=True,
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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def remote(monkeypatch):
    state = {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE},
             "headRefName": "", "title": "", "body": ""}
    real = subprocess.run

    def run(args, **kwargs):
        if args[0] == "gh":
            return subprocess.CompletedProcess(args, 0, json.dumps(rest_pr(state)).encode(), b"")
        if args[0] == "git" and len(args) > 1 and args[1] == "ls-remote":
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state


@pytest.fixture(params=["checkout-in-scratch", "home-tree"])
def shape(request):
    """``checkout-in-scratch``: t_3ecbb73d's ``repo/mainwt`` (refused by the walk).
    ``home-tree``: the workspace IS a repo with a linked worktree and a gitlink
    (refused by the registry scan)."""
    return request.param


def nested_card(conn, tmp_path, remote, shape):
    """A checkout carrying a linked worktree and a gitlink inside it."""
    tid = kb.create_task(conn, title="nested workspace")
    remote["headRefName"] = f"daedalus/{tid}-fix"
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    repo = _init(ws if shape == "home-tree" else ws / "repo")
    sub = _init(tmp_path / "sub")
    git(repo, "submodule", "add", str(sub), "vendor/pool")
    git(repo, "commit", "-m", "add submodule")
    git(repo, "worktree", "add", str(repo / "mainwt"), "-b", "side")
    (repo / "mainwt" / "code.py").write_text("value = 2\n")
    kbw.set_workspace_path(conn, tid, ws)
    return tid, ws


def repo_of(ws, shape):
    return ws if shape == "home-tree" else ws / "repo"


def test_merged_survivor_completes_beside_nested_repos(board, remote, tmp_path, shape):
    tid, ws = nested_card(board, tmp_path, remote, shape)

    assert kb.complete_task(board, tid, summary="approved", survivor_pr=PR)

    task = kb.get_task(board, tid)
    assert task.status == "done"
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "ref" and saved["refs"][0]["pr"] == PR
    assert saved["refs"][0]["sha"] == MERGE
    # Cleanup still refuses to delete the nested repository: the workspace is
    # HELD, not reaped, and the uncommitted edit inside it survives.
    assert (repo_of(ws, shape) / "mainwt" / "code.py").read_text() == "value = 2\n"
    held = board.execute("SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?",
                         (tid,)).fetchone()["held_reason"]
    assert "nested repository" in held


def test_no_survivor_still_refuses(board, remote, tmp_path, shape):
    tid, ws = nested_card(board, tmp_path, remote, shape)

    with pytest.raises(ValueError, match="nested repository"):
        kb.complete_task(board, tid, summary="approved",
                         metadata={"changed_files": ["code.py"]})
    assert kb.get_task(board, tid).status != "done"


def test_open_survivor_still_refuses(board, remote, tmp_path, shape):
    """Published is not landed: an OPEN PR keeps the refusal."""
    remote.update(state="OPEN", mergeCommit=None)
    tid, ws = nested_card(board, tmp_path, remote, shape)

    with pytest.raises(ValueError, match="nested repository"):
        kb.complete_task(board, tid, summary="approved", survivor_pr=PR)
    assert kb.get_task(board, tid).status != "done"


def _vanished_bases(board, tid, ws, shape):
    """Dispatch recorded three repositories; two have since vanished."""
    kept = str(repo_of(ws, shape).relative_to(ws)) or "."
    bases = {kept: HEAD, "gone1": HEAD, "gone2": HEAD}
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps(bases)),
        )


def test_landed_shortcut_still_requires_coverage_of_vanished_repos(board, remote, tmp_path, shape):
    """Prism 2e98d07648a3: a merged claim for ONE vanished repository must not
    vouch for a second vanished one just because a nested repo sits beside them."""
    tid, ws = nested_card(board, tmp_path, remote, shape)
    _vanished_bases(board, tid, ws, shape)

    with pytest.raises(ValueError, match="gone2"):
        kb.complete_task(board, tid, summary="approved", survivor_pr=[f"gone1={PR}"])
    assert kb.get_task(board, tid).status != "done"


def test_landed_shortcut_completes_when_every_vanished_repo_is_named(board, remote, tmp_path, shape):
    tid, ws = nested_card(board, tmp_path, remote, shape)
    _vanished_bases(board, tid, ws, shape)

    assert kb.complete_task(board, tid, summary="approved",
                            survivor_pr=[f"gone1={PR}", f"gone2={PR}"])
    assert kb.get_task(board, tid).status == "done"


def test_unqualified_claim_is_bound_to_the_single_vanished_repo(board, remote, tmp_path, shape):
    """Prism c7eccc58634e: an accepted unqualified claim must be recorded under the vanished
    repository, not under the workspace root ``.``."""
    tid, ws = nested_card(board, tmp_path, remote, shape)
    kept = str(repo_of(ws, shape).relative_to(ws)) or "."
    with kb.write_txn(board):
        board.execute(
            "INSERT INTO task_workspace_survivors(task_id, bases) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET bases = excluded.bases",
            (tid, json.dumps({kept: HEAD, "gone": HEAD})),
        )

    assert kb.complete_task(board, tid, summary="approved", survivor_pr=PR)
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert [r["repository"] for r in saved["refs"]] == ["gone"]


def test_directory_without_git_still_counts_as_vanished(board, remote, tmp_path, shape):
    """Prism 8e6fc262130b: ``gone2/`` left behind without its ``.git`` is still a vanished
    repository and still needs its own qualified claim."""
    tid, ws = nested_card(board, tmp_path, remote, shape)
    _vanished_bases(board, tid, ws, shape)
    (ws / "gone2").mkdir()
    (ws / "gone2" / "leftover.txt").write_text("x\n")

    with pytest.raises(ValueError, match="gone2"):
        kb.complete_task(board, tid, summary="approved", survivor_pr=[f"gone1={PR}"])
    assert kb.get_task(board, tid).status != "done"
