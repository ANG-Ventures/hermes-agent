"""A home-clone workspace closes against the LIVE tree, never against the mirror.

Round-2 contract (Apollo ruling, 2026-09-20, superseding this card's original
spec). The hermes-home "isolated remote sync" rewrites TREES, not just shas: the
card's own evidence pair 9f25d7cce / e747d18db shares a `git patch-id` while the
trees differ by 58 files. So:

  * "published on the mirror" is unsatisfiable as a content claim — do not chase
    it, and never let a patch-id hit authorise deleting a workspace;
  * the durable target is the LIVE CANONICAL TREE the workspace was cloned from
    (a local repo outside every kanban/temp root), which the fleet-backup tier
    covers;
  * patch-id survives only as an ADVISORY sidecar annotation.

These tests pin that split: the live tree is the deletion authority, and the
reviewer's collision reproducer (same diff, different base, an unpublished
commit) must fail closed.

TEST-REPIN: fdedf3fc2e6a21e808b0b8b9cd94557b46856c72 ANG-Ventures/hermes-agent#795 — merged survivor authority supersedes unconditional landed acceptance.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def git(repo, *args):
    return subprocess.run(
        [str(a) for a in ["git", "-C", str(repo), *args]], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


def commit(repo, name, text, message):
    (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init(repo, *, bare=False):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", *(["--bare"] if bare else []), "-b", "main")
    if not bare:
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
    return repo


def restore_artifact(survivor, tmp_path, published):
    """Restore either recovery shape and return its checkout."""
    restored = tmp_path / "restored"
    if survivor["kind"] == "bundle":
        git(tmp_path, "clone", survivor["bundles"][0]["path"], str(restored))
    else:
        git(tmp_path, "clone", "-b", "main", str(published), str(restored))
        manifest = json.loads(Path(survivor["sidecar"]).read_text())
        git(restored, "checkout", "--detach", manifest["repositories"][0]["base_sha"])
        git(restored, "apply", survivor["path"])
    return restored


@pytest.fixture
def board(tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def rewriting_mirror(repo, mirror, *, branch="main"):
    """Publish `repo` under DIFFERENT shas AND a DIFFERENT tree.

    This is the real hermes-home shape the ruling names: the sync does not merely
    re-date commits, it republishes a tree that is not a faithful copy (the live
    pair differs by 58 files). It must REWRITE history, not append to it — an
    appended "drop the file" commit would leave the local head an ANCESTOR of the
    mirror tip, which `_remote_survivor` accepts, making every test below vacuous.
    The diff of the tip commit is preserved across the rewrite, so the patch-id
    still matches — which is exactly why patch-id must stay advisory.
    """
    init(mirror, bare=True)
    staging = mirror.parent / f"{mirror.stem}-staging"
    git(mirror.parent, "clone", "--no-local", "-b", branch, str(repo), str(staging))
    git(staging, "config", "user.name", "Mirror Sync")
    git(staging, "config", "user.email", "sync@example.invalid")
    subprocess.run(
        ["git", "-C", str(staging), "filter-branch", "--force",
         "--index-filter", "git rm -q --cached --ignore-unmatch local-only.txt",
         "--env-filter",
         'export GIT_COMMITTER_NAME="Mirror Sync";'
         'export GIT_COMMITTER_EMAIL="sync@example.invalid";'
         'export GIT_COMMITTER_DATE="2001-02-03T04:05:06Z"',
         "--", "--all"],
        stdin=subprocess.DEVNULL, capture_output=True, check=True,
        env={**os.environ, "FILTER_BRANCH_SQUELCH_WARNING": "1"},
    )
    git(staging, "push", "--force", str(mirror), f"HEAD:refs/heads/{branch}")
    return git(staging, "rev-parse", "HEAD")


def home_clone(board, tmp_path, *, live_remote=True):
    """A workspace whose DURABLE remote is a rewriting mirror.

    Shape, matching the live card scenario (t_e69d693a):

      * ``origin`` -> the rewriting mirror. This is the only *durable* remote
        (``_durable_remote`` only accepts a local path when it is ``origin``),
        and it republishes every commit under a new sha over a different tree,
        so ``_remote_survivor`` can never match.
      * ``live``   -> the canonical tree the clone came from, when
        ``live_remote`` is set. Not durable (not ``origin``), so only
        ``_canonical_survivor`` can see it. This is the auto-escape.

    ``live_remote=False`` is the REAL hermes-home shape measured on the live
    box: ``~/.hermes/kanban/workspaces/t_e69d693a/code`` has exactly two
    remotes, both ``git@github.com:ANG-Ventures/hermes-home.git``, and no local
    path remote at all — so ``_canonical_repos`` is empty there and the explicit
    ``landed`` claim is the only escape. The tests below pin both halves.
    """
    live = init(tmp_path / "live-home")
    commit(live, "code.py", "value = 1\n", "base")
    (live / "local-only.txt").write_text("only here\n")
    git(live, "add", "-A")
    git(live, "commit", "-m", "local-only content")

    tid = kb.create_task(board, title="home clone work")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.parent.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "clone", "--no-local", str(live), str(ws))
    git(ws, "config", "user.name", "Worker")
    git(ws, "config", "user.email", "worker@example.invalid")
    head = commit(ws, "code.py", "value = 2\n", "implementation")

    # The work lands in the live tree (this is what "landed" means here).
    git(live, "fetch", str(ws), "main")
    git(live, "reset", "--hard", "FETCH_HEAD")
    mirror = tmp_path / "hermes-home.git"
    mirror_sha = rewriting_mirror(live, mirror)
    git(live, "remote", "add", "origin", str(mirror))

    git(ws, "remote", "set-url", "origin", str(mirror))
    if live_remote:
        git(ws, "remote", "add", "live", str(live))
    kb.set_workspace_path(board, tid, ws)
    return tid, ws, live, head, mirror_sha


def test_the_mirror_is_not_a_faithful_copy(board, tmp_path):
    """Premise check: if this stops holding, the tests below are vacuous."""
    from hermes_cli.kanban_survivor import _patch_id, _published_refs, _remote_survivor

    _, ws, live, head, mirror_sha = home_clone(board, tmp_path)
    probe = tmp_path / "mirror-probe"
    git(tmp_path, "clone", "-b", "main", str(tmp_path / "hermes-home.git"), str(probe))
    assert mirror_sha != head
    assert git(live, "rev-parse", f"{head}^{{tree}}") != git(probe, "rev-parse", f"{mirror_sha}^{{tree}}")
    assert (live / "local-only.txt").exists()
    assert not (probe / "local-only.txt").exists()
    # REWRITE, not append: the sha path must be genuinely unreachable here, or
    # every test below passes for the wrong reason.
    assert _remote_survivor(ws, head, list(_published_refs(ws, ws))) is None
    # The diff still matches — the advisory signal survives, the tree claim does not.
    assert _patch_id(ws, head) == _patch_id(probe, mirror_sha) is not None


def test_home_clone_closes_against_the_live_tree(board, tmp_path):
    """The whole point of the card: this workspace can now complete."""
    tid, ws, live, head, mirror_sha = home_clone(board, tmp_path)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "ref", survivor
    ref = survivor["refs"][0]
    assert ref["matched_by"] == "canonical"
    assert ref["repository_path"] == str(live.resolve())
    assert ref["sha"] == head
    assert not ws.exists()
    # The live tree really carries the work the workspace held.
    assert git(live, "rev-parse", "HEAD") == head
    # Advisory only: the mirror hint never stands alone as the accept reason.
    assert ref.get("mirror_hint", {}).get("advisory") is True
    assert json.loads(Path(survivor["sidecar"]).read_text())["refs"][0]["sha"] == head


def test_no_survivor_kind_permits_deletion_by_patch_id(board, tmp_path):
    """`ref-by-content` is gone: a diff match is never a deletion authority.

    This is the REAL hermes-home shape (`live_remote=False`): the only remote is
    the rewriting mirror, exactly as measured on
    `~/.hermes/kanban/workspaces/t_e69d693a/code`. Patch-id matches the mirror,
    and that must buy nothing.
    """
    import hermes_cli.kanban_survivor as survivor_mod
    from hermes_cli.kanban_survivor import (
        _canonical_repos, _canonical_survivor, _content_advisory, _published_refs,
    )

    assert not hasattr(survivor_mod, "_content_survivor")
    tid, ws, live, head, mirror_sha = home_clone(board, tmp_path, live_remote=False)
    # Premise: the live tree is NOT visible from this workspace, but the mirror
    # DOES carry the diff. (The fixture's mirror is a local bare repo, so it is
    # itself "canonical-shaped" — it just cannot reach `head`, because it
    # rewrote it. On the live box the mirror is a github URL and not local at
    # all.) What matters is that no canonical repo vouches for this commit.
    assert str(live.resolve()) not in [c["repository_path"] for c in _canonical_repos(ws, ws)]
    assert _canonical_survivor(ws, head, ws) is None
    assert _content_advisory(ws, head, list(_published_refs(ws, ws)))
    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] in {"patch", "bundle"}, survivor
    assert "ref-by-content" not in json.dumps(survivor)


def test_canonical_match_requires_reachability_from_the_live_head(board, tmp_path):
    """Mutation guard: drop the reachability check and this must stop passing.

    The live tree exists and is durable, but it does NOT carry this commit —
    presence of a canonical repo is not itself authority.
    """
    from hermes_cli.kanban_survivor import _canonical_survivor, _canonical_repos

    tid, ws, live, head, _ = home_clone(board, tmp_path)
    orphan = commit(ws, "code.py", "value = 99\n", "never reaches the live tree")
    assert orphan != head
    # The canonical repo is still discovered — only reachability rejects it.
    assert str(live.resolve()) in [c["repository_path"] for c in _canonical_repos(ws, ws)]
    assert _canonical_survivor(ws, orphan, ws) is None

    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] in {"patch", "bundle"}, survivor


def test_a_disposable_repo_is_never_canonical(board, tmp_path):
    """A workspace/temp tree cannot vouch for itself."""
    from hermes_cli.kanban_survivor import _canonical_repos

    tid, ws, live, head, _ = home_clone(board, tmp_path)
    sibling = init(tmp_path / "temporary" / "sibling")
    commit(sibling, "code.py", "value = 1\n", "base")
    git(ws, "remote", "set-url", "origin", str(sibling))
    git(ws, "remote", "remove", "live")
    assert list(_canonical_repos(ws, ws)) == []


# --- the reviewer's collision reproducer, as a committed regression test ---


def divergent_history(tmp_path):
    """Local HEAD is patch-identical to the published tip over a DIFFERENT tree.

    Published:  base -> fix
    Local:      base -> unpublished -> fix'   (same diff, new base)

    A diff-only check blesses this as published; `unpublished.py` exists nowhere
    durable. This is the shape that made round 1 a data-loss path.
    """
    source = init(tmp_path / "divergent-source")
    commit(source, "core.py", "value = 1\n", "base")
    base = git(source, "rev-parse", "HEAD")
    published_fix = commit(source, "fix.py", "y = 2\n", "fix")
    mirror = init(tmp_path / "divergent-mirror.git", bare=True)
    git(source, "push", str(mirror), "HEAD:refs/heads/main")

    local = tmp_path / "divergent-local"
    git(tmp_path, "clone", "--no-local", "-b", "main", str(source), str(local))
    git(local, "config", "user.name", "Worker")
    git(local, "config", "user.email", "worker@example.invalid")
    git(local, "remote", "remove", "origin")
    git(local, "reset", "--hard", base)
    commit(local, "unpublished.py", "secret_work = 1\n", "exists nowhere else")
    head = commit(local, "fix.py", "y = 2\n", "fix")
    git(local, "remote", "add", "origin", str(mirror))
    return local, head, published_fix, mirror


def test_same_diff_different_base_fails_closed(board, tmp_path):
    """The round-1 data-loss reproducer: completion must NOT delete this tree."""
    from hermes_cli.kanban_survivor import _patch_id, _published_refs, _canonical_survivor

    tid = kb.create_task(board, title="divergent work")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.parent.mkdir(parents=True, exist_ok=True)
    local, head, published_fix, mirror = divergent_history(tmp_path)
    # `cp -R src dst` NESTS when dst exists, which silently produced a
    # workspace with no `.git` at all (and a vacuously-passing test).
    assert not any(ws.iterdir())
    shutil.copytree(local, ws, symlinks=True, dirs_exist_ok=True)
    assert (ws / ".git").exists()
    kb.set_workspace_path(board, tid, ws)

    # Premise: the diffs ARE identical, so a patch-id check would match here.
    probe = tmp_path / "divergent-probe"
    git(tmp_path, "clone", "-b", "main", str(mirror), str(probe))
    assert _patch_id(ws, head) == _patch_id(probe, published_fix) is not None
    # And there is no live canonical tree vouching for it.
    assert _canonical_survivor(ws, head, ws) is None
    assert list(_published_refs(ws, ws)), "mirror must be a durable remote here"

    assert kb.complete_task(board, tid, metadata={"changed_files": ["fix.py"]})
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] in {"patch", "bundle"}, survivor
    # The commit that exists nowhere else is still recoverable from the artifact.
    restored = restore_artifact(survivor, tmp_path, mirror)
    assert (restored / "unpublished.py").read_text() == "secret_work = 1\n"


def test_landed_claim_needs_a_live_tree_that_reaches_the_sha(board, tmp_path):
    """CLASS-SWEEP: `landed` used the same diff-identity shape; it must not now."""
    tid, ws, live, head, _ = home_clone(board, tmp_path)
    local, divergent_head, _, _ = divergent_history(tmp_path)
    # Patch-identical to the mirror, unreachable from any live tree's HEAD.
    git(local, "checkout", "-q", "-b", "parked")
    git(local, "checkout", "-q", "main")
    git(local, "reset", "--hard", "HEAD~2")
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["fix.py"],
            "landed": [{"repo_path": str(local), "sha": divergent_head}],
        })
    assert ws.exists()
    assert kb.get_task(board, tid).status != "done"


def test_landed_accepts_the_live_home_tree_without_any_mirror_claim(board, tmp_path):
    """The card's actual scenario: work committed into ~/.hermes, mirror irrelevant."""
    tid, ws, live, head, _ = home_clone(board, tmp_path)
    assert kb.complete_task(board, tid, metadata={
        "changed_files": ["code.py"],
        "landed": [{"repo_path": str(live), "sha": head}],
    })
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "landed", survivor
    entry = survivor["landed"][0]
    assert entry["sha"] == head
    assert entry["matched_by"] == "canonical"
    # Any mirror annotation is advisory; it is never the accept reason and the
    # old `published_sha` field (which WAS the accept reason) is gone.
    assert "published_sha" not in entry
    assert entry.get("published", {}).get("matched_by") in {None, "sha", "patch-id"}
    assert not ws.exists()


def test_landed_rejects_patch_id_whitespace_collision(board, tmp_path):
    """Whitespace-normalized diffs can execute differently; never discard the original."""
    from hermes_cli.kanban_survivor import _patch_id

    source = init(tmp_path / "collision-source")
    commit(source, "implementation.py", "if False:\n    safe = True\n", "base")
    tid = kb.create_task(board, title="whitespace collision")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    git(tmp_path, "clone", "--no-local", str(source), str(ws))
    git(ws, "config", "user.name", "Workspace")
    git(ws, "config", "user.email", "workspace@example.invalid")
    work_head = commit(ws, "implementation.py", "if False:\n    safe = True\nresult = 42\n", "work")
    live = tmp_path / "collision-live"
    git(tmp_path, "clone", "--no-local", str(source), str(live))
    git(live, "config", "user.name", "Live")
    git(live, "config", "user.email", "live@example.invalid")
    live_head = commit(live, "implementation.py", "if False:\n    safe = True\n    result = 42\n", "landed")
    assert _patch_id(ws, work_head) == _patch_id(live, live_head)
    assert git(ws, "rev-parse", f"{work_head}^:implementation.py") == git(live, "rev-parse", f"{live_head}^:implementation.py")
    assert _execution_value(ws) == 42
    assert _execution_value(live) is None
    kb.set_workspace_path(board, tid, ws)
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["implementation.py"],
            "landed": [{"repo_path": str(live), "sha": live_head}],
        })
    assert ws.exists()
    assert _execution_value(ws) == 42


def _execution_value(repo):
    scope = {}
    exec((repo / "implementation.py").read_text(), scope)
    return scope.get("result")


def test_landed_rejects_a_valid_but_unrelated_commit(board, tmp_path):
    tid, ws, _, _, _ = home_clone(board, tmp_path)
    unrelated = init(tmp_path / "unrelated-live")
    unrelated_sha = commit(unrelated, "other.py", "other = True\n", "unrelated")

    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["code.py"],
            "landed": [{"repo_path": str(unrelated), "sha": unrelated_sha}],
        })

    assert ws.exists()
    assert (ws / "code.py").read_text() == "value = 2\n"


@pytest.mark.parametrize("dirty", ["tracked", "untracked"])
def test_landed_rejects_a_dirty_workspace(board, tmp_path, dirty):
    tid, ws, live, head, _ = home_clone(board, tmp_path)
    if dirty == "tracked":
        (ws / "code.py").write_text("value = 3\n")
    else:
        (ws / "untracked.py").write_text("only_here = True\n")

    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["code.py"],
            "landed": [{"repo_path": str(live), "sha": head}],
        })

    assert ws.exists()


def test_landed_allows_ignored_workspace_files(board, tmp_path):
    tid, ws, live, _, _ = home_clone(board, tmp_path)
    (ws / ".gitignore").write_text("scratch.log\n")
    git(ws, "add", ".gitignore")
    git(ws, "commit", "-m", "ignore scratch output")
    head = git(ws, "rev-parse", "HEAD")
    git(live, "fetch", str(ws), "main")
    git(live, "reset", "--hard", "FETCH_HEAD")
    (ws / "scratch.log").write_text("ignored\n")

    assert kb.complete_task(board, tid, metadata={
        "changed_files": [".gitignore"],
        "landed": [{"repo_path": str(live), "sha": head}],
    })
    assert not ws.exists()


def test_landed_accepts_byte_identical_rewritten_workspace_history(board, tmp_path):
    source = init(tmp_path / "patch-source")
    commit(source, "code.py", "value = 1\n", "base")
    tid = kb.create_task(board, title="rewritten landed history")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    git(tmp_path, "clone", "--no-local", str(source), str(ws))
    git(ws, "config", "user.name", "Workspace")
    git(ws, "config", "user.email", "workspace@example.invalid")
    workspace_head = commit(ws, "code.py", "value = 2\n", "workspace implementation")

    live = tmp_path / "patch-live"
    git(tmp_path, "clone", "--no-local", str(source), str(live))
    git(live, "config", "user.name", "Live")
    git(live, "config", "user.email", "live@example.invalid")
    landed_head = commit(live, "code.py", "value = 2\n", "rewritten implementation")
    assert landed_head != workspace_head
    kb.set_workspace_path(board, tid, ws)

    assert kb.complete_task(board, tid, metadata={
        "changed_files": ["code.py"],
        "landed": [{"repo_path": str(live), "sha": landed_head}],
    })
    entry = kb.latest_run(board, tid).metadata["survivor"]["landed"][0]
    assert entry["workspace_repositories"] == [{
        "repository": ".", "head": workspace_head, "matched_by": "exact-diff",
    }]
    assert not ws.exists()


@pytest.mark.parametrize("rewritten", [False, True])
def test_landed_rejects_work_reverted_from_canonical_head(board, tmp_path, rewritten):
    source = init(tmp_path / "reverted-source")
    commit(source, "implementation.py", "result = 0\n", "base")
    tid = kb.create_task(board, title="reverted landed work")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    git(tmp_path, "clone", "--no-local", str(source), str(ws))
    live = tmp_path / "reverted-live"
    git(tmp_path, "clone", "--no-local", str(source), str(live))
    for repo in (ws, live):
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
    work = commit(ws, "implementation.py", "result = 1\n", "workspace implementation")
    if rewritten:
        landed = commit(live, "implementation.py", "result = 1\n", "landed then removed")
        assert landed != work
        from hermes_cli.kanban_survivor import _exact_commit_diff
        assert _exact_commit_diff(ws, work) == _exact_commit_diff(live, landed)
    else:
        git(live, "fetch", str(ws), "main")
        git(live, "reset", "--hard", "FETCH_HEAD")
    reverted = commit(live, "implementation.py", "result = 0\n", "revert implementation")
    assert _execution_value(ws) == 1
    assert _execution_value(live) == 0
    kb.set_workspace_path(board, tid, ws)

    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["implementation.py"],
            "landed": [{"repo_path": str(live), "sha": reverted}],
        })
    assert ws.exists()
    assert _execution_value(ws) == 1
    assert kb.get_task(board, tid).status != "done"


def test_landed_allows_unrelated_canonical_addition(board, tmp_path):
    source = init(tmp_path / "addition-source")
    commit(source, "implementation.py", "result = 0\n", "base")
    tid = kb.create_task(board, title="canonical independent addition")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    git(tmp_path, "clone", "--no-local", str(source), str(ws))
    git(ws, "config", "user.name", "Test")
    git(ws, "config", "user.email", "test@example.invalid")
    work = commit(ws, "implementation.py", "result = 1\n", "implementation")
    live = tmp_path / "addition-live"
    git(tmp_path, "clone", "--no-local", str(source), str(live))
    git(live, "config", "user.name", "Test")
    git(live, "config", "user.email", "test@example.invalid")
    git(live, "fetch", str(ws), "main")
    git(live, "reset", "--hard", "FETCH_HEAD")
    commit(live, "independent.py", "other = 2\n", "unrelated addition")
    kb.set_workspace_path(board, tid, ws)
    assert kb.complete_task(board, tid, metadata={
        "changed_files": ["implementation.py"],
        "landed": [{"repo_path": str(live), "sha": work}],
    })
    assert not ws.exists()
    assert _execution_value(live) == 1


@pytest.mark.parametrize("restored", [False, True])
def test_landed_checks_deleted_paths_and_modes_at_live_head(board, tmp_path, restored):
    source = init(tmp_path / "mode-source")
    commit(source, "implementation.py", "result = 1\n", "base")
    tid = kb.create_task(board, title="deleted path or mode reverted")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    git(tmp_path, "clone", "--no-local", str(source), str(ws))
    live = tmp_path / "mode-live"
    git(tmp_path, "clone", "--no-local", str(source), str(live))
    for repo in (ws, live):
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
    if restored:
        git(ws, "update-index", "--chmod=+x", "implementation.py")
        git(ws, "commit", "-m", "make executable")
    else:
        (ws / "implementation.py").unlink()
        git(ws, "add", "-u")
        git(ws, "commit", "-m", "remove implementation")
    git(live, "fetch", str(ws), "main")
    git(live, "reset", "--hard", "FETCH_HEAD")
    if restored:
        git(live, "update-index", "--chmod=-x", "implementation.py")
        git(live, "commit", "-m", "remove executable mode")
    else:
        commit(live, "implementation.py", "result = 1\n", "restore deleted path")
    kb.set_workspace_path(board, tid, ws)
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["implementation.py"],
            "landed": [{"repo_path": str(live), "sha": git(live, "rev-parse", "HEAD")}],
        })
    assert ws.exists()
    assert kb.get_task(board, tid).status != "done"


def test_landed_rejects_a_disposable_repository(board, tmp_path):
    """Pointing `landed` at the workspace itself must not authorise its deletion."""
    tid, ws, live, head, _ = home_clone(board, tmp_path)
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["code.py"],
            "landed": [{"repo_path": str(ws), "sha": head}],
        })
    assert ws.exists()


def test_landed_rejects_a_directory_that_is_not_a_repository(board, tmp_path):
    tid, ws, live, head, _ = home_clone(board, tmp_path)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["code.py"],
            "landed": [{"repo_path": str(plain), "sha": head}],
        })
    assert ws.exists()


def test_landed_rejects_an_unbound_nested_repository(board, tmp_path):
    """A root-repo receipt cannot discard a nested repo's independent objects."""
    tid, ws, live, head, _ = home_clone(board, tmp_path, live_remote=False)
    nested = init(ws / "nested")
    nested_head = commit(nested, "nested.py", "only_here = True\n", "nested implementation")
    git(ws, "add", "nested")
    git(ws, "commit", "-m", "record nested repository")
    root_head = git(ws, "rev-parse", "HEAD")
    git(live, "fetch", str(ws), "main")
    git(live, "reset", "--hard", "FETCH_HEAD")

    with pytest.raises(ValueError, match="survivor_unavailable"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["nested/nested.py"],
            "landed": [{"repo_path": str(live), "sha": root_head}],
        })

    assert ws.exists()
    assert git(nested, "rev-parse", "HEAD") == nested_head
    assert (nested / "nested.py").read_text() == "only_here = True\n"


def test_landed_cleanup_rechecks_live_reachability(board, tmp_path):
    """A stored receipt cannot authorize deletion after its live ref disappears."""
    from hermes_cli.kanban_survivor import preserve, remove_workspace_dir
    tid, ws, live, head, _ = home_clone(board, tmp_path, live_remote=False)
    preserve(board, tid, {"landed": [{"repo_path": str(live), "sha": head}]})
    git(live, "reset", "--hard", "HEAD~1")
    assert not remove_workspace_dir(board, tid, ws)
    assert ws.exists()


def test_diff_collision_over_attachment_limit_holds_workspace(board, tmp_path, monkeypatch):
    """A matching patch-id cannot bypass the cap by claiming a durable ref."""
    tid = kb.create_task(board, title="oversized divergent work")
    ws, _, _, _ = divergent_history(tmp_path)
    kb.set_workspace_path(board, tid, ws)
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="exceeds attachment limit"):
        kb.complete_task(board, tid, metadata={"changed_files": ["unpublished.py"]})
    assert kb.get_task(board, tid).status != "done"
    assert (ws / "unpublished.py").read_text() == "secret_work = 1\n"


def stored_survivor(conn, tid):
    """The DURABLE recovery row -- what a later recovery actually reads."""
    import hermes_cli.kanban_survivor as survivor_mod
    return survivor_mod._state(conn, tid)[2]


def assert_truthful_recovery_row(survivor):
    """A row may only call itself a patch/bundle if it POINTS AT bytes.

    The class this pins: a recovery row whose `kind`/`notice`/`patches` promise
    an artifact that does not exist is worse than a mislabelled one -- the
    workspace is already deleted by then, so the index is the only map back to
    the implementation and it now points nowhere.
    """
    if survivor.get("kind") == "patch":
        path = survivor.get("path")
        assert path, f"kind=patch with no patch path: {survivor}"
        assert Path(path).exists(), f"kind=patch pointing at missing bytes: {path}"
    if survivor.get("kind") == "bundle":
        bundles = survivor.get("bundles") or ()
        assert bundles, f"kind=bundle with no bundles: {survivor}"
        for bundle in bundles:
            assert Path(bundle["path"]).exists(), bundle
    # A displaced pointer is retained because it is the only copy of some
    # work: it must name real bytes (a patch `path`, or a bundle row's
    # manifest whose `bundles` were merged into the fresh row), never a bare
    # metadata manifest.
    for pointer in (survivor.get("patches") or ()):
        path = pointer.get("path")
        assert path, f"displaced recovery pointer with no patch path: {pointer}"
        assert Path(path).exists(), f"displaced pointer to missing bytes: {path}"
    if survivor.get("notice") == "NOT PUSHED":
        assert survivor.get("path") or survivor.get("bundles"), (
            f"NOT PUSHED promises unpushed bytes this row does not hold: {survivor}")


def test_landed_cleanup_keeps_a_truthful_row_when_the_manifest_changes(board, tmp_path):
    """A re-captured metadata sidecar is not a second PATCH to carry forward.

    Completion and cleanup are separate calls, and cleanup re-verifies the
    claim and re-stores `implementation.json`. Because the row carries that
    manifest in the same `sidecar` slot a real patch uses, the non-shrink guard
    read the completion's manifest as a stored patch being dropped, carried it
    forward, and relabelled the durable row `kind: "patch"` / `NOT PUSHED` with
    a `patches` list of JSON manifests holding no patch bytes -- after the
    workspace had already been deleted.
    """
    from hermes_cli.kanban_survivor import preserve, remove_workspace_dir

    tid, ws, live, head, _ = home_clone(board, tmp_path, live_remote=False)
    first = preserve(board, tid, {"landed": [{"repo_path": str(live), "sha": head}]})
    assert first["kind"] == "landed", first

    # Anything that changes the re-stored manifest reaches this path; a branch
    # rename is the cheapest (`_verify_landed` records the live branch).
    git(live, "branch", "-m", "renamed")

    assert remove_workspace_dir(board, tid, ws)
    assert not ws.exists()

    stored = stored_survivor(board, tid)
    assert stored["kind"] == "landed", stored
    assert "patches" not in stored, stored
    assert stored.get("notice") != "NOT PUSHED", stored
    assert stored["landed"][0]["sha"] == head, stored
    assert_truthful_recovery_row(stored)


def test_canonical_ref_cleanup_keeps_a_truthful_row_when_the_manifest_changes(board, tmp_path):
    """Same class, the OTHER sidecar-bearing kind this PR added.

    The canonical `ref` arm writes `implementation.json` for exactly the same
    reason `landed` does -- to name the live tree holding a sha the published
    remote lacks -- so it is exposed to the identical mislabelling.
    """
    from hermes_cli.kanban_survivor import preserve

    tid, ws, live, head, _ = home_clone(board, tmp_path)
    first = preserve(board, tid, {"changed_files": ["code.py"]})
    assert first["kind"] == "ref" and first.get("sidecar"), first

    # Advance the work so the re-capture's manifest genuinely differs (a
    # stable manifest re-stores to the SAME path and never reaches the carry
    # path -- that is the coverage boundary the previous round tested).
    second_head = commit(ws, "code.py", "value = 3\n", "more implementation")
    git(live, "fetch", str(ws), "main")
    git(live, "reset", "--hard", "FETCH_HEAD")
    second = preserve(board, tid, {"changed_files": ["code.py"]})
    assert second["kind"] == "ref", second
    assert second["sidecar"] != first["sidecar"], (first, second)
    assert second["refs"][0]["sha"] == second_head, second
    assert "patches" not in second, second
    assert second.get("notice") != "NOT PUSHED", second
    assert_truthful_recovery_row(stored_survivor(board, tid))


def test_a_real_patch_is_still_carried_when_a_recapture_drops_it(board, tmp_path):
    """Positive control: narrowing the sidecar rule must not inert non-shrink.

    If this ever fails, the fix above has traded a mislabelled row for actual
    data loss -- the case the non-shrink guard exists for.
    """
    import hermes_cli.kanban_survivor as survivor_mod

    tid, ws, live, head, _ = home_clone(board, tmp_path, live_remote=False)
    (ws / "unpublished.py").write_text("secret_work = 1\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-m", "unpublished implementation")
    captured = survivor_mod.preserve(board, tid, {"changed_files": ["unpublished.py"]})
    assert captured["kind"] in ("patch", "bundle"), captured
    assert_truthful_recovery_row(captured)

    # The work then lands in the live tree, so a re-capture emits a bare ref
    # and would otherwise drop the only copy of the pre-landing bytes.
    git(live, "fetch", str(ws), "main")
    git(live, "reset", "--hard", "FETCH_HEAD")
    survivor_mod.preserve(board, tid, {"landed": [
        {"repo_path": str(live), "sha": git(ws, "rev-parse", "HEAD")}]})

    stored = stored_survivor(board, tid)
    assert stored["kind"] in ("patch", "bundle"), stored
    assert stored.get("notice") == "NOT PUSHED", stored
    assert_truthful_recovery_row(stored)
