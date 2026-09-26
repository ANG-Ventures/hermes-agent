"""Survivors for workspaces whose only remote REWRITES the shas it publishes.

A home clone (`~/.hermes` mirrored to ANG-Ventures/hermes-home by the "isolated
remote sync") can never match `_remote_survivor`: the mirror republishes every
commit under a new sha. Before this, such a workspace fell through to the
patch/bundle path and a 94 MB home tree always tripped
``KANBAN_ATTACHMENT_MAX_BYTES`` — blocking completion of work that was committed
and running (2026-09-20, t_e69d693a). Under the round-2 ruling, patch-id is only
an advisory diff match; live canonical reachability is the landed authority.

TEST-REPIN: fdedf3fc2e6a21e808b0b8b9cd94557b46856c72 ANG-Ventures/hermes-agent#795 — merged survivor authority supersedes artifact-shape and inert mutation assertions.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
        # filter-branch otherwise sleeps 10s on its deprecation warning, once
        # per rewriting_mirror() call (~130s of this file's CI time).
        env={**os.environ, "FILTER_BRANCH_SQUELCH_WARNING": "1"},
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


def restore_artifact(survivor, tmp_path, published):
    restored = tmp_path / "artifact-restored"
    if survivor["kind"] == "bundle":
        git(tmp_path, "clone", survivor["bundles"][0]["path"], str(restored))
    else:
        git(tmp_path, "clone", "-b", "main", str(published), str(restored))
        manifest = json.loads(Path(survivor["sidecar"]).read_text())
        git(restored, "checkout", "--detach", manifest["repositories"][0]["base_sha"])
        git(restored, "apply", survivor["path"])
    return restored


def rewriting_mirror(repo, mirror, *, branch="main"):
    """Publish repo's history to `mirror` under DIFFERENT shas, same content.

    This fixture rewrites identity only. The live sync also changes trees;
    test_kanban_survivor_live_tree.py covers that stricter case.
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


def test_rewritten_mirror_alone_requires_a_recovery_bundle(board, tmp_path):
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    # Round-2 ruling: a rewritten diff is only an advisory, not a survivor.
    assert survivor["kind"] == "bundle", survivor
    manifest = json.loads(Path(survivor["sidecar"]).read_text())
    assert manifest["bundles"]
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
    restored = restore_artifact(survivor, tmp_path, tmp_path / "hermes-home.git")
    assert (restored / "code.py").read_text() == "value = 3\n"
    mirror = tmp_path / "mirror-control"
    git(tmp_path, "clone", "-b", "main", str(tmp_path / "hermes-home.git"), str(mirror))
    assert (mirror / "code.py").read_text() == "value = 2\n"


def test_content_match_requires_the_patch_id_check(board, tmp_path, monkeypatch):
    """Mutation guard: patch-id is what creates the advisory mirror hint."""
    import hermes_cli.kanban_survivor as survivor_mod
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    published = list(survivor_mod._published_refs(ws, ws))
    hint = survivor_mod._content_advisory(ws, local, published)
    assert hint and hint["sha"] == remote_sha
    monkeypatch.setattr(survivor_mod, "_patch_id", lambda *a, **k: None)
    assert survivor_mod._content_advisory(ws, local, published) is None


def test_content_scan_keeps_credential_bearing_url_out_of_fetch_argv(board, tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor_mod
    tid, ws, local, _ = home_clone_task(board, tmp_path)
    raw_url = "https://user:secret@example.invalid/private.git"
    real_git = survivor_mod._git
    fetch_calls = []

    def recording_git(repo, *args, **kwargs):
        if args[:3] == ("remote", "get-url", "origin"):
            return subprocess.CompletedProcess([], 0, raw_url.encode(), b"")
        if "fetch" in args:
            fetch_calls.append((args, kwargs.get("env", {})))
            return subprocess.CompletedProcess([], 1, b"", b"")
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(survivor_mod, "_git", recording_git)
    assert survivor_mod._content_advisory(
        ws, local, [{"remote": "origin", "branch": "main", "sha": "0" * 40}],
    ) is None
    assert len(fetch_calls) == 1
    args, env = fetch_calls[0]
    assert all(raw_url not in str(arg) for arg in args)
    assert env["KANBAN_FETCH_URL"] == raw_url
    assert "candidate" in args


def test_patch_id_is_stable_across_a_sha_rewrite(board, tmp_path):
    """The advisory signal survives an identity-only rewrite."""
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
    assert entry["published"]["sha"] == landed_sha
    assert entry["matched_by"] == "canonical"
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
    assert entry["matched_by"] == "canonical"
    assert entry["sha"] == local
    assert entry["published"]["sha"] == remote_sha
    assert entry["published"]["advisory"] is True


@pytest.mark.parametrize("break_it", ["disposable", "unreachable", "missing_repo", "no_such_sha"])
def test_landed_escape_fails_closed_on_every_unverifiable_claim(board, tmp_path, break_it):
    """A `landed` claim authorises deleting the code; it must never be taken on trust."""
    tid, ws, local, remote_sha = home_clone_task(board, tmp_path)
    live = tmp_path / "live-home"
    git(tmp_path, "clone", "-b", "main", str(tmp_path / "hermes-home.git"), str(live))
    git(live, "config", "user.name", "Live")
    git(live, "config", "user.email", "live@example.invalid")
    if break_it == "disposable":
        sha = local
        repo_path = str(ws)
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
        _content_advisory, _published_refs, _CONTENT_SCAN_DEPTH,
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
    assert _content_advisory(local, head, list(_published_refs(local, local))) is None
