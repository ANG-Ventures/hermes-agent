"""t_b15005c7: a SHALLOW workspace clone must never surface a bare `git bundle failed (rc=128)`.

`_snapshot(base=None)` cuts the bundle in a throwaway `recovery.git` that
borrows the workspace's objects through GIT_ALTERNATE_OBJECT_DIRECTORIES. The
borrowed store does not carry the source's `shallow` grafts, so `bundle create
--all` walked into parents the clone never fetched and died rc=128, which no
classifier explained. Two shapes are pinned here:

* clean HEAD that the remote has since moved past: HEAD is still an ancestor of
  the live branch, so it is PUBLISHED and the card completes with a `ref`
  survivor (no bundle is needed at all);
* unpublished local work on a shallow clone: the bundle is cut against the
  shallow boundary and the card completes with a bundle that verifies in a
  shallow clone of the same remote.
"""
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw


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


def _identity(repo):
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")


def shallow_workspace(conn, tmp_path):
    """A card whose workspace is a `--depth 1` clone; the remote then advances.

    Returns (task id, workspace, remote, upstream). `bases` stays empty: the
    workspace path is set before the clone exists, so nothing is recorded.
    """
    tid = kb.create_task(conn, title="shallow clone")
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kbw.set_workspace_path(conn, tid, ws)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    upstream = tmp_path / "upstream"
    git(tmp_path, "init", "-b", "main", str(upstream))
    _identity(upstream)
    for i in range(4):
        (upstream / f"f{i}.txt").write_text(f"{i}\n")
        git(upstream, "add", ".")
        git(upstream, "commit", "-m", f"c{i}")
    git(upstream, "remote", "add", "origin", str(remote))
    git(upstream, "push", "origin", "main")
    clone = ws / "repo"
    git(ws, "clone", "-q", "--depth", "1", f"file://{remote}", str(clone))
    _identity(clone)
    assert git(clone, "rev-parse", "--is-shallow-repository") == "true"
    # The remote moves on: its tip is now a commit the clone never fetched.
    (upstream / "later.txt").write_text("later\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "remote advanced")
    git(upstream, "push", "origin", "main")
    return tid, ws, remote, clone


def test_clean_shallow_head_behind_an_advanced_remote_is_published(board, tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    # A `file://` remote doubles as a canonical local tree; a real fleet remote
    # (https/ssh) never does. Disable that path so the remote is judged only by
    # what it advertises, as it is in production.
    monkeypatch.setattr(survivor, "_canonical_survivor", lambda *a, **k: None)
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    head = git(clone, "rev-parse", "HEAD")
    assert kb.complete_task(board, tid)
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "ref", survivor
    assert survivor["refs"][0]["head"] == head
    assert survivor["refs"][0]["matched_by"] == "live-ancestor"
    assert not kb.list_attachments(board, tid)
    assert not ws.exists()


def test_unpublished_work_on_a_shallow_clone_is_bundled(board, tmp_path):
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    # Make the published tip unreachable from this clone's view: commit local
    # work on top of the shallow HEAD, and drop the remote's new tip from the
    # advertised set by rewinding nothing -- the new tip is simply absent.
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local work")
    work = git(clone, "rev-parse", "HEAD")
    assert kb.complete_task(board, tid)
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] in {"bundle", "patch"}, survivor
    attachments = kb.list_attachments(board, tid)
    stored = [Path(a.stored_path) for a in attachments
              if a.stored_path.endswith((".bundle", ".patch"))]
    assert stored, attachments
    artifact = stored[0]
    # Restore from the remote + the artifact alone.
    restored = tmp_path / "restored"
    git(tmp_path, "clone", "-q", f"file://{remote}", str(restored))
    _identity(restored)
    if artifact.suffix == ".bundle":
        git(restored, "fetch", "-q", str(artifact), "refs/heads/implementation:refs/heads/recovered")
        git(restored, "checkout", "-q", "recovered")
        assert git(restored, "rev-parse", "HEAD^") == work
    else:
        git(restored, "apply", "-p2", str(artifact))
    assert (restored / "work.txt").read_text() == "unpublished\n"


def test_a_shallow_bundle_declares_its_boundary_as_prerequisites(board, tmp_path):
    """A shallow bundle omits everything behind the boundary; it must say so.

    Without declared prerequisites `bundle verify` calls it complete in an
    EMPTY repo, yet `clone`/`fetch` there die "Failed to traverse parents".
    Declared, an empty repo refuses it up front, and the remote's history
    plus the bundle restore the work.
    """
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local work")
    work = git(clone, "rev-parse", "HEAD")
    boundary = set((clone / ".git" / "shallow").read_text().split())
    assert kb.complete_task(board, tid)
    stored = [Path(a.stored_path) for a in kb.list_attachments(board, tid)
              if a.stored_path.endswith(".bundle")]
    assert stored, kb.list_attachments(board, tid)
    artifact = stored[0]
    listed = subprocess.run(["git", "bundle", "list-heads", str(artifact)],
                            capture_output=True, check=True).stdout.decode()
    assert "refs/heads/implementation" in listed
    header = artifact.read_bytes().split(b"\n\n", 1)[0].decode()
    prereqs = {line[1:].split()[0] for line in header.splitlines() if line.startswith("-")}
    assert prereqs == boundary, header
    # An empty repo cannot satisfy the boundary: verify says so instead of "okay".
    empty = tmp_path / "empty"
    git(tmp_path, "init", "-q", str(empty))
    verify = subprocess.run(["git", "-C", str(empty), "bundle", "verify", str(artifact)],
                            capture_output=True)
    assert verify.returncode != 0
    assert b"lacks these prerequisite commits" in verify.stderr, verify.stderr
    # Fetch the remote's history into that same empty repo, then the bundle restores.
    git(empty, "fetch", "-q", f"file://{remote}", "+refs/heads/*:refs/remotes/origin/*")
    git(empty, "bundle", "verify", str(artifact))
    git(empty, "fetch", "-q", str(artifact), "refs/heads/implementation:refs/heads/recovered")
    git(empty, "checkout", "-q", "recovered")
    assert git(empty, "rev-parse", "HEAD^") == work
    assert (empty / "work.txt").read_text() == "unpublished\n"


def test_a_stale_shallow_entry_whose_object_is_absent_still_bundles(board, tmp_path):
    """A `shallow` line naming an object the store lacks is not a boundary.

    Passed through as `^<sha>`, `bundle create` dies rc=128 "bad object"; it is
    dropped, and the real boundary is still declared.
    """
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local work")
    shallow = clone / ".git" / "shallow"
    boundary = set(shallow.read_text().split())
    with shallow.open("a") as fh:
        fh.write("1" * 40 + "\n")
    assert kb.complete_task(board, tid)
    stored = [Path(a.stored_path) for a in kb.list_attachments(board, tid)
              if a.stored_path.endswith(".bundle")]
    assert stored, kb.list_attachments(board, tid)
    header = stored[0].read_bytes().split(b"\n\n", 1)[0].decode()
    prereqs = {line[1:].split()[0] for line in header.splitlines() if line.startswith("-")}
    assert prereqs == boundary, header


def test_a_shallow_capture_that_still_fails_names_the_remedy(board, tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    # Simulate any future shallow-traversal failure: without the grafts the
    # bundle walks past the boundary exactly as it did before the fix.
    monkeypatch.setattr(survivor, "_borrow_shallow", lambda *a, **k: None)
    # Without grafts the live-boundary probe also fails; skip it so this pins
    # the bundle-traversal failure, not the boundary refusal.
    monkeypatch.setattr(survivor, "_require_live_boundary", lambda *a, **k: None)
    tid, ws, remote, clone = shallow_workspace(board, tmp_path)
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local work")
    with pytest.raises(survivor.SurvivorUnavailable) as raised:
        kb.complete_task(board, tid)
    message = str(raised.value)
    assert "shallow clone ./repo" in message
    assert "fetch --unshallow" in message
    assert ws.exists()


def _one_commit_shallow_workspace(conn, tmp_path):
    """Depth-1 clone of a one-commit upstream (Prism d722f02857e3 repro)."""
    tid = kb.create_task(conn, title="shallow, upstream gone")
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(parents=True, exist_ok=True)
    kbw.set_workspace_path(conn, tid, ws)
    upstream = tmp_path / "upstream"
    git(tmp_path, "init", "-q", "-b", "main", str(upstream))
    _identity(upstream)
    (upstream / "base.txt").write_text("base\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-q", "-m", "only")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(upstream), str(remote))
    clone = ws / "repo"
    git(ws, "clone", "-q", "--depth", "1", f"file://{remote}", str(clone))
    _identity(clone)
    assert git(clone, "rev-parse", "--is-shallow-repository") == "true"
    return tid, ws, remote, upstream, clone


def _commit_work(clone):
    (clone / "work.txt").write_text("unpublished\n")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "local work")


def test_shallow_boundary_on_no_durable_remote_keeps_the_workspace(board, tmp_path):
    """Prism d722f02857e3: the upstream holding the boundary is gone.

    The bundle omits the boundary tree and its unchanged blobs, so nothing can
    restore it; complete_task must refuse and keep the workspace.
    """
    import shutil

    import hermes_cli.kanban_survivor as survivor
    tid, ws, remote, upstream, clone = _one_commit_shallow_workspace(board, tmp_path)
    shutil.rmtree(remote)
    shutil.rmtree(upstream)
    git(clone, "remote", "remove", "origin")
    _commit_work(clone)
    with pytest.raises(survivor.SurvivorUnavailable) as raised:
        kb.complete_task(board, tid)
    message = str(raised.value)
    assert "not reachable from any durable remote" in message, message
    # The boundary is gone upstream: `fetch --unshallow` cannot help, so it is not offered.
    assert "push the work to a durable remote" in message, message
    assert "fetch --unshallow" not in message, message
    assert ws.exists()
    assert (clone / "work.txt").read_text() == "unpublished\n"


def test_shallow_boundary_force_pushed_away_keeps_the_workspace(board, tmp_path):
    """The remote still exists, but its branch no longer contains the boundary."""
    import hermes_cli.kanban_survivor as survivor
    tid, ws, remote, upstream, clone = _one_commit_shallow_workspace(board, tmp_path)
    git(upstream, "checkout", "-q", "--orphan", "rewrite")
    (upstream / "other.txt").write_text("other\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-q", "-m", "unrelated")
    git(upstream, "push", "-q", "--force", str(remote), "rewrite:main")
    git(remote, "gc", "-q", "--prune=now")
    _commit_work(clone)
    with pytest.raises(survivor.SurvivorUnavailable) as raised:
        kb.complete_task(board, tid)
    assert "not reachable from any durable remote" in str(raised.value)
    assert ws.exists()
