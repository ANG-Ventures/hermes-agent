"""A clean, pinned, checked-out submodule is not a nested repository to recover.

Card t_adf672fb. t_327d1c1b's scratch workspace held ``sat/`` (a clone of the
PR's repo) whose ``esp-libopus`` submodule had been initialised by the build.
``preserve()`` saw two repositories, one inside the other, and refused with
``survivor_unavailable: nested repository requires separate recovery`` ahead
of every survivor-consulting branch -- so even a REST-verified merged
``--survivor-pr`` with ``--survivor-unbound`` had no legal path to done.

A submodule whose HEAD is exactly the gitlink its parent records, with no
dirty, untracked or ignored bytes, and whose HEAD a remote-tracking ref holds,
carries nothing the parent's survivor does not already name. It is dropped
from the nested refusal. Any other nested repository still fails closed.

Mutation check: make ``_pinned_submodule`` return False -> the ``*_completes``
tests go red; make it return True -> the ``*_still_refuses`` tests go red.
"""
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as survivor

NESTED = "nested repository requires separate recovery"


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, capture_output=True, check=True,
    ).stdout.decode().strip()


def _init(repo, name="code.py"):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / name).write_text("value = 1\n")
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
def upstream(tmp_path):
    """A project repo whose tree pins a library submodule, as on GitHub."""
    lib = _init(tmp_path / "remotes" / "lib", "lib.c")
    project = _init(tmp_path / "remotes" / "project")
    git(project, "submodule", "add", str(lib), "components/lib")
    git(project, "commit", "-m", "pin lib")
    return project


def _clone_with_submodule(upstream, dest):
    git(dest.parent, "clone", str(upstream), dest.name)
    git(dest, "config", "user.name", "Test")
    git(dest, "config", "user.email", "test@example.invalid")
    git(dest, "submodule", "update", "--init")
    sub = dest / "components" / "lib"
    assert (sub / ".git").exists(), "fixture: submodule must be checked out"
    return dest, sub


def _card(conn, ws):
    return kb.create_task(conn, title="scratch card", assignee="daedalus",
                          workspace_kind="scratch", workspace_path=str(ws))


def _complete(conn, tid, ws):
    return survivor.preserve(conn, tid, {"changed_files": ["sat/code.py"]}, workspace=ws)


def _captured(result):
    manifest = json.loads(Path(result["sidecar"]).read_text())
    return {e["repository"] for e in manifest.get("repositories", [])}


@pytest.fixture
def scratch(tmp_path, upstream):
    """The reported shape: scratch dir (not a repo) holding one clone."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    sat, sub = _clone_with_submodule(upstream, ws / "sat")
    (sat / "code.py").write_text("value = 2\n")  # the card's own work
    return ws, sat, sub


# --- clean pinned submodules no longer refuse --------------------------------


def test_clean_pinned_submodule_in_scratch_clone_completes(board, scratch):
    ws, _sat, _sub = scratch
    tid = _card(board, ws)

    result = _complete(board, tid, ws)

    assert result is not None and result["kind"] in {"bundle", "patch"}, result
    assert _captured(result) == {"sat"}, result
    _bases, held, _prev = survivor._state(board, tid)
    assert held is None


def test_clean_pinned_submodule_of_workspace_root_completes(board, tmp_path, upstream):
    """Registry arm: the workspace itself is the clone, the gitlink is indexed."""
    ws, _sub = _clone_with_submodule(upstream, tmp_path / "rootclone")
    (ws / "code.py").write_text("value = 2\n")
    assert survivor._registered_nested(ws), "fixture: git must register the submodule"
    tid = _card(board, ws)

    result = survivor.preserve(board, tid, {"changed_files": ["code.py"]}, workspace=ws)

    assert _captured(result) == {"."}, result


# --- anything that could hold bytes still refuses ----------------------------


def test_dirty_submodule_still_refuses(board, scratch):
    ws, _sat, sub = scratch
    (sub / "lib.c").write_text("value = 3\n")
    tid = _card(board, ws)
    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, ws)
    assert NESTED in str(caught.value)


def test_untracked_file_in_submodule_still_refuses(board, scratch):
    ws, _sat, sub = scratch
    (sub / "notes.txt").write_text("only here\n")
    tid = _card(board, ws)
    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, ws)
    assert NESTED in str(caught.value)


def test_ignored_file_in_submodule_still_refuses(board, scratch):
    ws, _sat, sub = scratch
    info = Path(git(sub, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("*.bin\n")
    (sub / "build.bin").write_text("artifact\n")
    tid = _card(board, ws)
    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, ws)
    assert NESTED in str(caught.value)


def test_submodule_commit_beyond_gitlink_still_refuses(board, scratch):
    """Local commit in the submodule: HEAD no longer equals the pinned sha."""
    ws, _sat, sub = scratch
    git(sub, "config", "user.name", "Test")
    git(sub, "config", "user.email", "test@example.invalid")
    (sub / "lib.c").write_text("value = 4\n")
    git(sub, "commit", "-am", "local only")
    tid = _card(board, ws)
    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, ws)
    assert NESTED in str(caught.value)


def test_pinned_but_unpublished_submodule_commit_still_refuses(board, scratch):
    """Gitlink moved to a commit no remote-tracking ref holds."""
    ws, sat, sub = scratch
    git(sub, "config", "user.name", "Test")
    git(sub, "config", "user.email", "test@example.invalid")
    (sub / "lib.c").write_text("value = 5\n")
    git(sub, "commit", "-am", "local only")
    git(sat, "add", "components/lib")
    git(sat, "commit", "-m", "bump pin to unpublished commit")
    tid = _card(board, ws)
    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, ws)
    assert NESTED in str(caught.value)


def test_plain_hand_clone_inside_clone_still_refuses(board, scratch):
    """No gitlink at all: an ordinary nested clone keeps the old refusal."""
    ws, sat, _sub = scratch
    _init(sat / "vendor" / "handmade")
    tid = _card(board, ws)
    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        _complete(board, tid, ws)
    assert NESTED in str(caught.value)
