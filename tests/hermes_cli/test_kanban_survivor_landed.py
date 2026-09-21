"""Survivors for workspaces whose only remote REWRITES the shas it publishes.

A home clone (`~/.hermes` mirrored to ANG-Ventures/hermes-home by the "isolated
remote sync") can never match `_remote_survivor`: the mirror republishes every
commit under a new sha. Before this, such a workspace fell through to the
patch/bundle path and a 94 MB home tree always tripped
``KANBAN_ATTACHMENT_MAX_BYTES`` — blocking completion of work that was committed
and running (2026-09-20, t_e69d693a). Content identity (`git patch-id`) is what
survives the rewrite; these tests pin that it is verified, not assumed.
"""
import json
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


def commit(repo, name, text, message):
    (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def rewriting_mirror(repo, mirror, *, branch="main"):
    """Publish repo's history to `mirror` under DIFFERENT shas, same content.

    This is what the isolated remote sync does: identical trees and identical
    diffs, but rewritten committer identity/dates, so every sha differs.
    """
    git(repo, "init", "--bare", str(mirror))
    staging = mirror.parent / f"{mirror.stem}-staging"
    git(mirror.parent, "clone", "--no-local", "-b", branch, str(repo), str(staging))
    git(staging, "config", "user.name", "Mirror Sync")
    git(staging, "config", "user.email", "sync@example.invalid")
    git(staging, "-c", "rebase.instructionFormat=%s", "filter-branch", "--force",
        "--env-filter",
        'export GIT_COMMITTER_NAME="Mirror Sync";'
        'export GIT_COMMITTER_EMAIL="sync@example.invalid";'
        'export GIT_COMMITTER_DATE="2001-02-03T04:05:06Z"',
        "--", "--all")
    git(staging, "push", "--force", str(mirror), f"HEAD:refs/heads/{branch}")
    return git(staging, "rev-parse", "HEAD")


def home_clone_task(conn, tmp_path, *, publish=True):
    """A workspace that is a clone of a repo published through a rewriting mirror."""
    source = tmp_path / "home-source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    commit(source, "code.py", "value = 1\n", "base")
    tid = kb.create_task(conn, title="home clone work")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    ws.parent.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "clone", "--no-local", str(source), str(ws))
    git(ws, "config", "user.name", "Worker")
    git(ws, "config", "user.email", "worker@example.invalid")
    git(ws, "remote", "remove", "origin")
    local = commit(ws, "code.py", "value = 2\n", "implementation")
    mirror = tmp_path / "hermes-home.git"
    if publish:
        # The worker's commit reaches the mirror, but with a rewritten sha.
        git(source, "fetch", str(ws), "main")
        git(source, "reset", "--hard", "FETCH_HEAD")
        remote_sha = rewriting_mirror(source, mirror)
        assert remote_sha != local
    else:
        git(source, "init", "--bare", str(mirror))
        git(source, "push", str(mirror), "HEAD:refs/heads/main")
        remote_sha = None
    git(ws, "remote", "add", "origin", str(mirror))
    kb.set_workspace_path(conn, tid, ws)
    return tid, ws, local, remote_sha


def test_rewritten_mirror_completes_by_content_instead_of_oversized_bundle(board, tmp_path):
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "ref-by-content", survivor
    ref = survivor["refs"][0]
    assert ref["matched_by"] == "patch-id"
    assert ref["head"] == local
    assert ref["sha"] == remote_sha != local
    # The manifest is the only record tying the local sha to the published one.
    manifest = json.loads(Path(survivor["sidecar"]).read_text())
    assert manifest["refs"][0]["sha"] == remote_sha
    assert not ws.exists()
    # The published content really is the implementation.
    restored = tmp_path / "restored"
    git(tmp_path, "clone", "-b", "main", str(tmp_path / "hermes-home.git"), str(restored))
    assert (restored / "code.py").read_text() == "value = 2\n"


def test_unpublished_commit_still_fails_closed_on_a_rewriting_mirror(board, tmp_path):
    """The mirror is reachable but does NOT carry this content: keep the code."""
    tid, ws, local, _ = home_clone_task(board, tmp_path)
    commit(ws, "code.py", "value = 3\n", "never published")
    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] != "ref-by-content", survivor
    assert survivor["kind"] in {"patch", "bundle"}


def test_content_match_requires_the_patch_id_check(board, tmp_path, monkeypatch):
    """Mutation guard: neuter the patch-id comparison and the escape must close.

    If `_patch_id` stops discriminating (always None), no content match can be
    proven and the capture must fall back to the artifact path rather than
    silently blessing an unpublished head.
    """
    import hermes_cli.kanban_survivor as survivor_mod
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    monkeypatch.setattr(survivor_mod, "_patch_id", lambda *a, **k: None)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] in {"patch", "bundle"}, survivor


def test_patch_id_is_stable_across_a_sha_rewrite(board, tmp_path):
    """The property the whole escape rests on, asserted directly."""
    from hermes_cli.kanban_survivor import _patch_id
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    mirror_work = tmp_path / "mirror-work"
    git(tmp_path, "clone", "-b", "main", str(tmp_path / "hermes-home.git"), str(mirror_work))
    assert git(mirror_work, "rev-parse", "HEAD") == remote_sha != local
    assert _patch_id(ws, local) == _patch_id(mirror_work, remote_sha) is not None


def test_landed_escape_accepts_a_verified_published_commit(board, tmp_path):
    """`landed` lets a worker point at a repo the workspace merely mirrors."""
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    # A second clone standing in for the live home tree the work landed in.
    live = tmp_path / "live-home"
    git(tmp_path, "clone", "-b", "main", str(tmp_path / "hermes-home.git"), str(live))
    landed_sha = git(live, "rev-parse", "HEAD")
    assert kb.complete_task(board, tid, metadata={
        "changed_files": ["code.py"],
        "landed": [{"repo_path": str(live), "sha": landed_sha}],
    })
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "landed", survivor
    entry = survivor["landed"][0]
    assert entry["sha"] == landed_sha
    assert entry["published_sha"] == landed_sha
    assert entry["matched_by"] == "sha"
    assert json.loads(Path(survivor["sidecar"]).read_text())["landed"][0]["sha"] == landed_sha
    assert not ws.exists()


def test_landed_escape_accepts_a_content_match_on_a_rewriting_mirror(board, tmp_path):
    """The real hermes-home shape: local sha absent from the mirror, content present."""
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    live = tmp_path / "live-home-local-shas"
    git(tmp_path, "clone", "--no-local", "-b", "main", str(ws), str(live))
    git(live, "remote", "set-url", "origin", str(tmp_path / "hermes-home.git"))
    assert git(live, "rev-parse", "HEAD") == local
    assert remote_sha != local
    assert kb.complete_task(board, tid, metadata={
        "changed_files": ["code.py"],
        "landed": [{"repo_path": str(live), "sha": local}],
    })
    entry = kb.latest_run(board, tid).metadata["survivor"]["landed"][0]
    assert entry["matched_by"] == "patch-id"
    assert entry["sha"] == local
    assert entry["published_sha"] == remote_sha


@pytest.mark.parametrize("break_it", ["unpublished", "unreachable", "missing_repo", "no_such_sha"])
def test_landed_escape_fails_closed_on_every_unverifiable_claim(board, tmp_path, break_it):
    """A `landed` claim authorises deleting the code; it must never be taken on trust."""
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    live = tmp_path / "live-home"
    git(tmp_path, "clone", "-b", "main", str(tmp_path / "hermes-home.git"), str(live))
    git(live, "config", "user.name", "Live")
    git(live, "config", "user.email", "live@example.invalid")
    if break_it == "unpublished":
        sha = commit(live, "code.py", "value = 99\n", "landed but never pushed")
        repo_path = str(live)
    elif break_it == "unreachable":
        # PUBLISHED but on an abandoned branch HEAD cannot reach: not this line
        # of work. Publication alone must not satisfy the claim, so this commit
        # is pushed to the mirror — only the reachability check can reject it.
        git(live, "checkout", "-q", "-b", "sidebranch")
        sha = commit(live, "side.py", "side = True\n", "orphan work")
        git(live, "push", str(tmp_path / "hermes-home.git"), "HEAD:refs/heads/sidebranch")
        git(live, "checkout", "-q", "main")
        repo_path = str(live)
    elif break_it == "missing_repo":
        sha, repo_path = git(live, "rev-parse", "HEAD"), str(tmp_path / "not-a-repo")
    else:
        sha, repo_path = "0" * 40, str(live)
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["code.py"],
            "landed": [{"repo_path": repo_path, "sha": sha}],
        })
    assert kb.get_task(board, tid).status != "done"
    assert ws.exists()
    assert any(e.kind == "workspace_held" for e in kb.list_events(board, tid))


def test_landed_escape_rejects_a_malformed_claim(board, tmp_path):
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["code.py"], "landed": [{"sha": local}],
        })
    assert ws.exists()


def test_content_scan_does_not_match_the_shallow_boundary_commit(board, tmp_path):
    """The deepest fetched commit diffs against nothing; it must never match.

    `git diff-tree --root` on a shallow boundary yields the entire tree as an
    addition, which collides with any unpublished ROOT commit carrying the same
    content — blessing it as published and authorising deletion of the only
    copy. The boundary must be fetched (so real commits are complete) but never
    scanned.
    """
    import shutil
    from hermes_cli.kanban_survivor import (
        _content_survivor, _published_refs, _CONTENT_SCAN_DEPTH,
    )
    source = tmp_path / "deep-source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    for i in range(_CONTENT_SCAN_DEPTH + 6):
        commit(source, f"file{i}.py", f"n = {i}\n", f"commit {i}")
    mirror = tmp_path / "deep-mirror.git"
    git(source, "init", "--bare", str(mirror))
    git(source, "push", str(mirror), "HEAD:refs/heads/main")
    # The commit the probe's shallow fetch will land on as its boundary.
    boundary = git(source, "rev-parse", f"HEAD~{_CONTENT_SCAN_DEPTH}")

    # An UNPUBLISHED local root commit whose tree equals the boundary's tree.
    # Its --root diff is byte-identical to the boundary's would-be --root diff.
    local = tmp_path / "boundary-twin"
    local.mkdir()
    git(source, "worktree", "add", "--detach", str(tmp_path / "at-boundary"), boundary)
    for item in (tmp_path / "at-boundary").iterdir():
        if item.name != ".git":
            (shutil.copytree if item.is_dir() else shutil.copy2)(item, local / item.name)
    git(local, "init", "-b", "main")
    git(local, "config", "user.name", "Test")
    git(local, "config", "user.email", "test@example.invalid")
    git(local, "add", "-A")
    git(local, "commit", "-m", "unpublished root with the boundary's content")
    git(local, "remote", "add", "origin", str(mirror))
    head = git(local, "rev-parse", "HEAD")
    assert head != boundary

    # The verdict that matters: this content is NOT published, so no survivor.
    assert _content_survivor(local, head, list(_published_refs(local, local))) is None
