"""The survivor patch base must not fall behind the card's recorded base.

t_93fba703: four dir-worktree cards forked at origin/main 9cbc8c50d, one commit
each, produced 5.2 MB patches carrying 111 files of main's own history. The
remote's live `main` tip was not in the local object store, so `_base` could
only rank the published heads it DID hold -- an older branch at 50a3180d1, an
ancestor of the fork point -- and cut the patch there. The recorded base
(`task_workspace_survivors.bases`) was the fork point all along.
"""
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


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def stale_base_workspace(conn, tmp_path, *, track_main=True, rewrite_main=False, prune=True,
                         remote_name="origin"):
    """old -> fork (recorded base) -> work; remote main has moved past `fork`."""
    tid = kb.create_task(conn, title="implement fixture")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(exist_ok=True)
    git(ws, "init", "-b", "main")
    git(ws, "config", "user.name", "Test")
    git(ws, "config", "user.email", "test@example.invalid")
    old = commit(ws, "code.py", "value = 1\n")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(ws, "remote", "add", remote_name, str(remote))
    git(ws, "push", remote_name, "HEAD:refs/heads/old")
    fork = commit(ws, "history.txt", "main's own history\n")
    if track_main:
        git(ws, "push", remote_name, "HEAD:refs/heads/main")
        git(ws, "fetch", remote_name)
    kb.set_workspace_path(conn, tid, ws)       # records bases[.] = fork
    # Main moves on elsewhere; its new tip never reaches this object store.
    other = tmp_path / "other"
    git(tmp_path, "clone", "-b", "old", str(remote), str(other))
    git(other, "config", "user.name", "Test")
    git(other, "config", "user.email", "test@example.invalid")
    if track_main and not rewrite_main:
        git(other, "fetch", "origin", "main")
        git(other, "reset", "--hard", "origin/main")
    if rewrite_main:
        # Live main force-pushed to unrelated history and the old commits
        # pruned: the local tracking ref still (stale-ly) contains `fork`.
        git(other, "checkout", "-q", "--orphan", "rewritten")
        git(other, "rm", "-rq", "--cached", ".")
    commit(other, "later.txt", "later main\n")
    git(other, "push", "-f", "origin", "HEAD:refs/heads/main")
    if rewrite_main and prune:
        git(remote, "reflog", "expire", "--expire=now", "--all")
        git(remote, "gc", "-q", "--prune=now")
    work = commit(ws, "card.py", "card = True\n")
    return tid, ws, old, fork, work


def patch_of(conn, tid):
    patch = next(a for a in kb.list_attachments(conn, tid) if a.filename == "implementation.patch")
    return Path(patch.stored_path).read_text()


# A remote name may contain `/` (FleetReview P1 on #1449): the tracking ref
# refs/remotes/team/origin/main must resolve to remote team/origin, branch main.
@pytest.mark.parametrize("remote_name", ["origin", "team/origin"])
def test_patch_is_cut_against_recorded_base_not_older_published_head(board, tmp_path, monkeypatch,
                                                                    remote_name):
    if remote_name != "origin":
        # Only `origin` may be a local-path durable remote; stand in for a
        # hosted remote so the slash name reaches the base selection.
        import hermes_cli.kanban_survivor as survivor
        monkeypatch.setattr(survivor, "_durable_remote", lambda *a, **k: True)
    tid, ws, old, fork, work = stale_base_workspace(board, tmp_path, remote_name=remote_name)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["card.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "patch"
    data = patch_of(board, tid)
    headers = [line for line in data.splitlines() if line.startswith("diff --git ")]
    assert headers == ["diff --git a/card.py b/card.py"]
    assert f"base={fork}" in data
    # Recoverable from the remote alone: the fork point is published history.
    restored = tmp_path / "restored"
    git(tmp_path, "clone", "-b", "main", str(tmp_path / "remote.git"), str(restored))
    git(restored, "checkout", "-q", fork)
    patch = next(a for a in kb.list_attachments(board, tid) if a.filename == "implementation.patch")
    git(restored, "apply", "-p1", patch.stored_path)
    assert (restored / "card.py").read_text() == "card = True\n"


def test_unpublished_recorded_base_is_not_trusted(board, tmp_path):
    # The recorded base never reached any remote-tracking ref of a published
    # branch, so a patch against it could not be applied by a recoverer.
    tid, ws, old, fork, work = stale_base_workspace(board, tmp_path, track_main=False)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["card.py"]})
    data = patch_of(board, tid)
    assert f"base={old}" in data
    assert "diff --git a/history.txt b/history.txt" in data


@pytest.mark.parametrize("prune", [True, False], ids=["pruned", "orphan-still-stored"])
def test_recorded_base_off_the_live_branch_is_not_trusted(board, tmp_path, prune):
    # The local tracking ref still contains the recorded base, but the live
    # branch was force-pushed to unrelated history. Pruned or not (a server
    # keeps orphaned objects until gc), a fresh clone of the branch cannot
    # reach it, so the patch must stay on a base the live branch serves.
    tid, ws, old, fork, work = stale_base_workspace(board, tmp_path, rewrite_main=True,
                                                    prune=prune)
    if not prune:
        assert git(tmp_path / "remote.git", "cat-file", "-t", fork) == "commit"
    assert git(ws, "merge-base", "--is-ancestor", fork, "refs/remotes/origin/main") == ""
    assert kb.complete_task(board, tid, metadata={"changed_files": ["card.py"]})
    data = patch_of(board, tid)
    assert f"base={old}" in data
    assert "diff --git a/history.txt b/history.txt" in data


def test_live_probes_share_one_time_budget(tmp_path, monkeypatch):
    # FleetReview P1 on #1449: with the recorded base on many advertised
    # branches and a hung remote, each probe must not get a fresh 120 s.
    import hermes_cli.kanban_survivor as survivor
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    git(ws, "config", "user.name", "Test")
    git(ws, "config", "user.email", "test@example.invalid")
    base = commit(ws, "a.txt", "a\n")
    recorded = commit(ws, "b.txt", "b\n")
    commit(ws, "c.txt", "c\n")
    published = [{"remote": "origin", "branch": f"b{i}", "sha": "0" * 40} for i in range(60)]
    for ref in published:
        git(ws, "update-ref", f"refs/remotes/origin/{ref['branch']}", recorded)
    clock = {"now": 1000.0}
    import time
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    budgets = []

    def hung(repo, remote, branch, sha, *, timeout=survivor._LIVE_BASE_TIMEOUT):
        budgets.append(timeout)
        clock["now"] += timeout          # the fetch burns its whole budget
        return False

    monkeypatch.setattr(survivor, "_reachable_from_live", hung)
    assert survivor._recorded_base(ws, recorded, published, base) == base
    assert budgets and sum(budgets) <= survivor._LIVE_BASE_TIMEOUT


def test_live_probe_matches_sha256_object_format(tmp_path):
    # FleetReview P1 on #1449: a SHA-1 probe cannot hold a 64-hex base.
    import hermes_cli.kanban_survivor as survivor
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "--object-format=sha256", str(remote)],
                   check=True)
    ws = tmp_path / "ws"
    subprocess.run(["git", "init", "-q", "-b", "main", "--object-format=sha256", str(ws)],
                   check=True)
    git(ws, "config", "user.name", "Test")
    git(ws, "config", "user.email", "test@example.invalid")
    recorded = commit(ws, "a.txt", "a\n")
    commit(ws, "b.txt", "b\n")
    assert len(recorded) == 64
    git(ws, "remote", "add", "origin", str(remote))
    git(ws, "push", "-q", "origin", "HEAD:refs/heads/trunk")
    assert survivor._reachable_from_live(ws, "origin", "trunk", recorded) is True
