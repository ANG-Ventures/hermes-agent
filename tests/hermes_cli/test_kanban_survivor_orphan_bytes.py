"""Ignored orphan bytes of a lost repository identity must not be reaped.

Argus round 4 of PR #924 (card t_60592755) found two data-loss paths that
pre-date that PR (present on its merge-base 4a8affef73 and on its head):

P8  A recorded repository is re-initialised IN PLACE (``rm -rf a/.git`` then
    ``git init a``) and the new repository ignores the old, committed but
    unpublished file. ``_repos`` still returns ``a``, ``bases - keys`` is empty
    and ``status --untracked-files=all`` is clean, so a landed claim for the
    NEW repository completed the card and the workspace was deleted; neither
    the live repository nor any stored bundle held the bytes.

P9  A recorded child repository vanishes and a replacement root ignores its
    directory. An UNBOUND ``--survivor-pr`` for it completes the card (that
    one completion is what the override is for), but the same call's cleanup
    counted the unbound ref as coverage and ``rmtree``d the ignored bytes --
    the ``_reusable`` one-completion-only contract, bypassed through the
    ``missing <= absent`` relaxation.

Every case drives the real ``kb.complete_task`` (completion + its immediate
workspace cleanup) or the real reclamation gate; only remote lookups are
stubbed, the same way the repo's other survivor tests do it.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as ks

HEAD = "a1" * 20
PR = "example/project#68"
_REAL_RUN = subprocess.run


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The live repositories below sit under tmp_path; treat only a sibling as
    # temporary so they count as independent storage, as a real checkout would.
    monkeypatch.setattr(ks, "_temporary_roots", lambda: [tmp_path / "temporary"])
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def remote(monkeypatch):
    """A live OPEN PR; its branch name decides whether it is bound to the card."""
    state = {"state": "OPEN", "headRefOid": HEAD, "mergeCommit": None,
             "headRefName": "someone/unrelated-work", "title": "", "body": ""}

    def run(args, **kwargs):
        if args and args[0] == "gh":
            return subprocess.CompletedProcess(args, 0, json.dumps(state).encode(), b"")
        if args and args[0] == "git" and "ls-remote" in args and "-C" not in args:
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return _REAL_RUN(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return state


def _git(repo, *args):
    return _REAL_RUN(["git", "-C", str(repo), *map(str, args)], capture_output=True,
                     text=True, check=True).stdout.strip()


def _init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "QA")
    _git(repo, "config", "user.email", "qa@example.invalid")
    return repo


def _commit(repo, name, text):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _dispatched(board, tid):
    """Workspace with recorded repo `a` at a commit holding unpublished bytes."""
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    a = _init(ws / "a")
    _commit(a, "lost_impl.py", f"UNPUBLISHED_{tid}\n")
    kb.set_workspace_path(board, tid, ws)
    ks.record_baseline(board, tid, ws)
    assert set(ks._state(board, tid)[0]) == {"a"}
    return ws, a


def _replace_in_place(a, *, ignore):
    """P8 shape: same key, new repository; the old file stays on disk."""
    shutil.rmtree(a / ".git")
    _init(a)
    _commit(a, ".gitignore", ignore)
    return _commit(a, "new.py", "replacement\n")


def _live_clone(tmp_path, repo, name):
    live = tmp_path / name
    _REAL_RUN(["git", "clone", "-q", "--no-local", str(repo), str(live)], check=True)
    return live


def _held(board, tid):
    return ks._state(board, tid)[1]


# --- P8: identity replaced in place, old bytes ignored ------------------------

def test_replaced_identity_with_ignored_bytes_holds_landed_completion(board, remote, tmp_path):
    tid = kb.create_task(board, title="p8 landed")
    ws, a = _dispatched(board, tid)
    new_head = _replace_in_place(a, ignore="lost_impl.py\n")
    # The premise of the defect: every pre-existing check reads clean.
    assert _git(a, "status", "--porcelain", "--untracked-files=all") == ""
    live = _live_clone(tmp_path, a, "live-a")
    assert not (live / "lost_impl.py").exists()

    with pytest.raises(ValueError, match="replaced in place"):
        kb.complete_task(board, tid, metadata={
            "changed_files": ["a/lost_impl.py", "a/new.py"],
            "landed": [{"repo_path": str(live), "sha": new_head}]})

    assert kb.get_task(board, tid).status != "done"
    assert (a / "lost_impl.py").read_text(encoding="utf-8") == f"UNPUBLISHED_{tid}\n"
    assert "lost_impl.py" in _held(board, tid)


def test_replaced_identity_with_ignored_bytes_holds_ordinary_completion(board, remote):
    """Class sweep: the non-landed capture exit certified the new repo too."""
    tid = kb.create_task(board, title="p8 ordinary")
    ws, a = _dispatched(board, tid)
    _replace_in_place(a, ignore="lost_impl.py\n")

    with pytest.raises(ValueError, match="replaced in place"):
        kb.complete_task(board, tid, metadata={"changed_files": ["a/new.py"]})

    assert kb.get_task(board, tid).status != "done"
    assert (a / "lost_impl.py").exists()


def test_replaced_identity_with_ignored_bytes_holds_reclamation(board, remote):
    """Class sweep: a replacement AFTER completion must not be reaped either.

    Completion stored a bundle of the original repository. The worker then
    re-initialised `a` and kept editing the now-ignored file: those edits exist
    in no survivor, and the reclamation capture only sees the new repository.
    """
    tid = kb.create_task(board, title="p8 reclaim")
    ws, a = _dispatched(board, tid)
    assert ks.preserve(board, tid, {"changed_files": ["a/lost_impl.py"]})["kind"] == "bundle"
    _replace_in_place(a, ignore="lost_impl.py\n")
    (a / "lost_impl.py").write_text("EDITED_AFTER_COMPLETION\n", encoding="utf-8")

    assert ks.remove_workspace_dir(board, tid, ws) is False
    assert (a / "lost_impl.py").read_text(encoding="utf-8") == "EDITED_AFTER_COMPLETION\n"
    assert "replaced in place" in _held(board, tid)


def test_replaced_identity_that_commits_the_old_bytes_completes(board, remote, tmp_path):
    """Positive control (P8c): nothing ignored, so the landed repo carries it."""
    tid = kb.create_task(board, title="p8c")
    ws, a = _dispatched(board, tid)
    new_head = _replace_in_place(a, ignore="*.tmp\n")
    live = _live_clone(tmp_path, a, "live-a")
    assert (live / "lost_impl.py").read_text(encoding="utf-8") == f"UNPUBLISHED_{tid}\n"

    kb.complete_task(board, tid, metadata={
        "changed_files": ["a/lost_impl.py", "a/new.py"],
        "landed": [{"repo_path": str(live), "sha": new_head}]})

    assert kb.get_task(board, tid).status == "done"
    assert not ws.exists()
    assert _held(board, tid) is None


def test_replaced_identity_ignoring_only_derived_dirs_completes(board, remote, tmp_path):
    """Scope of the discriminator: build output is not evidence to vouch for."""
    tid = kb.create_task(board, title="p8 derived")
    ws, a = _dispatched(board, tid)
    (a / "lost_impl.py").unlink()
    new_head = _replace_in_place(a, ignore="node_modules/\n__pycache__/\n")
    (a / "node_modules" / "pkg").mkdir(parents=True)
    (a / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    live = _live_clone(tmp_path, a, "live-a")

    kb.complete_task(board, tid, metadata={
        "changed_files": ["a/new.py"], "landed": [{"repo_path": str(live), "sha": new_head}]})

    assert kb.get_task(board, tid).status == "done"
    assert not ws.exists()


def test_same_identity_repo_with_ignored_local_files_completes(board, remote, tmp_path):
    """Scope of the discriminator: ignored files alone are ordinary (.env, scratch).

    Only a repository that can no longer reach its dispatch commit is treated as
    a replaced identity; the same repository ignoring local files completes.
    """
    tid = kb.create_task(board, title="same identity")
    ws, a = _dispatched(board, tid)
    _commit(a, ".gitignore", "local.env\n")
    (a / "local.env").write_text("TOKEN=x\n", encoding="utf-8")
    head = _commit(a, "more.py", "more\n")
    live = _live_clone(tmp_path, a, "live-a")

    kb.complete_task(board, tid, metadata={
        "changed_files": ["a/more.py"], "landed": [{"repo_path": str(live), "sha": head}]})

    assert kb.get_task(board, tid).status == "done"
    assert not ws.exists()


# --- P9: vanished repo, ignored by the replacement root, unbound claim --------

def _vanish_under_ignoring_root(ws, a):
    shutil.rmtree(a / ".git")
    _init(ws)
    _commit(ws, ".gitignore", "a/\n")
    return _commit(ws, "replacement.txt", "replacement B\n")


def test_unbound_claim_completes_but_its_cleanup_keeps_ignored_bytes(board, remote):
    tid = kb.create_task(board, title="p9 unbound")
    ws, a = _dispatched(board, tid)
    _vanish_under_ignoring_root(ws, a)

    kb.complete_task(board, tid, metadata={"changed_files": ["a/lost_impl.py", "replacement.txt"]},
                     survivor_pr=PR, survivor_unbound=True)

    # The one completion the override authorises still happens...
    assert kb.get_task(board, tid).status == "done"
    # ...but it buys no deletion: the vanished repo's bytes are still here.
    assert (a / "lost_impl.py").read_text(encoding="utf-8") == f"UNPUBLISHED_{tid}\n"
    assert "unbound" in _held(board, tid)


def test_bound_claim_for_vanished_repo_reclaims(board, remote):
    """Positive control: per-repository authority bound to the card deletes."""
    tid = kb.create_task(board, title="p9 bound")
    remote["headRefName"] = f"operator/{tid}-landed-elsewhere"
    ws, a = _dispatched(board, tid)
    _vanish_under_ignoring_root(ws, a)

    kb.complete_task(board, tid, metadata={"changed_files": ["a/lost_impl.py", "replacement.txt"]},
                     survivor_pr=PR)

    assert kb.get_task(board, tid).status == "done"
    assert not ws.exists()
    assert _held(board, tid) is None
