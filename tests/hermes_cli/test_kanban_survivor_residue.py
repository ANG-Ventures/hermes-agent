"""A done card's tree whose only dirt is probe residue is removed; real edits still hold (t_71ed4ae8).

kanban/logs/workspace-deletions.log held ~910 `survivor-held-workspace` + ~590 remote refusals because
`remove_workspace_dir` counted __pycache__ / .venv / .pytest_cache as unpreserved work. Residue is now
rmtree'd with the tree (never preserved); tracked edits, non-residue untracked files, stashes, unpushed
commits and skip-worktree / assume-unchanged flags still hold, locally and in the remote ssh proof.
"""
import ast
import re
import shlex
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_survivor as ks


_RUN = subprocess.run  # the remote fixture patches subprocess.run module-wide


def git(repo, *args):
    return _RUN(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, check=True).stdout.decode().strip()


def residue(repo):
    (repo / "pkg" / "__pycache__").mkdir(parents=True)
    (repo / "pkg" / "__pycache__" / "code.cpython-312.pyc").write_bytes(b"\x00pyc")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("#!venv\n")
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "README.md").write_text("cache\n")
    (repo / "stray.pyc").write_bytes(b"\x00")


def make_repo(repo, remote):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / "code.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "HEAD:main")
    git(repo, "fetch", "-q", "origin")


# ------------------------------------------------------------------ local proof (complete -> cleanup)

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def local_card(conn):
    tid = kb.create_task(conn, title="residue fixture")
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    make_repo(ws, Path.home() / f"{tid}.git")
    kbw.set_workspace_path(conn, tid, ws)
    return tid, ws


def test_local_residue_only_tree_is_deleted_at_completion(board):
    tid, ws = local_card(board)
    residue(ws)
    assert kb.complete_task(board, tid)
    assert not ws.exists()
    survivor = kb.latest_run(board, tid).metadata["survivor"]
    assert survivor["kind"] == "ref"  # residue is never preserved as a patch
    assert not kb.list_attachments(board, tid)


def test_local_residue_removed_by_the_deleter(board):
    tid, ws = local_card(board)
    residue(ws)
    assert ks.remove_workspace_dir(board, tid, ws)
    assert not ws.exists()
    assert not kb.list_attachments(board, tid)  # residue is rmtree'd, never preserved


def test_local_mutation_without_residue_branch_holds(board, monkeypatch):
    """Mutation: with the residue classifier dropped, the same fixture is captured as work again."""
    monkeypatch.setattr(ks, "_is_residue", lambda path: False)
    tid, ws = local_card(board)
    residue(ws)
    assert kb.complete_task(board, tid)
    assert kb.latest_run(board, tid).metadata["survivor"]["kind"] == "patch"


@pytest.mark.parametrize("dirt", ["tracked", "untracked_py", "skip_worktree", "pycache_named_file"])
def test_local_real_edit_beside_residue_is_not_clean(board, dirt):
    tid, ws = local_card(board)
    residue(ws)
    if dirt == "tracked":
        (ws / "code.py").write_text("value = 2\n")
    elif dirt == "untracked_py":
        (ws / "notes.py").write_text("keep me\n")
    elif dirt == "skip_worktree":
        git(ws, "update-index", "--skip-worktree", "code.py")
        (ws / "code.py").write_text("hidden = 1\n")
    else:
        (ws / "__pycache__").write_text("a FILE named like a residue dir\n")
    assert not ks._residue_only(ws)
    kb.complete_task(board, tid)
    survivor = kb.latest_run(board, tid).metadata.get("survivor") or {}
    assert survivor.get("kind") != "ref", survivor


# ------------------------------------------------------------------ remote proof (python -c over ssh)

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(kb.SCHEMA_SQL)
    c.execute("INSERT INTO tasks(id,title,assignee,status,created_at,workspace_path,origin_host,body) "
              "VALUES('t_11111111','t','alpha','done',1,'/missing','ace-media','requires: none')")
    # Execute the exact remote payload locally, not a synthesized result.
    monkeypatch.setattr(ks.subprocess, "run", lambda argv, **kw: _RUN(shlex.split(argv[-1]), **kw))
    yield c
    c.close()


HOST = SimpleNamespace(name="fixture", target="unused", ssh_port=22)


def remote_ws(tmp_path):
    ws = tmp_path / "ws"
    make_repo(ws, tmp_path / "origin.git")
    return ws


def test_remote_residue_only_tree_is_deleted(db, tmp_path):
    ws = remote_ws(tmp_path)
    residue(ws)
    (ws / ".gitignore").write_text("node_modules/\n")  # ignored residue (!!) also counts
    git(ws, "add", ".gitignore")
    git(ws, "commit", "-qm", "ignore")
    git(ws, "push", "-q", "origin", "HEAD:main")
    (ws / "node_modules" / "x").mkdir(parents=True)
    (ws / "node_modules" / "x" / "index.js").write_text("1\n")
    assert ks.remove_workspace_dir(db, "t_11111111", ws, remote_host=HOST)
    assert not ws.exists()


@pytest.mark.parametrize("dirt", ["tracked", "untracked_py", "ignored_env", "skip_worktree", "stash"])
def test_remote_real_dirt_beside_residue_holds(db, tmp_path, dirt):
    ws = remote_ws(tmp_path)
    residue(ws)
    if dirt == "tracked":
        (ws / "code.py").write_text("value = 2\n")
    elif dirt == "untracked_py":
        (ws / "notes.py").write_text("keep me\n")
    elif dirt == "ignored_env":
        (ws / ".git" / "info" / "exclude").write_text(".env\n")
        (ws / ".env").write_text("not residue\n")
    elif dirt == "skip_worktree":
        git(ws, "update-index", "--skip-worktree", "code.py")
        (ws / "code.py").write_text("hidden = 1\n")
    else:
        (ws / "code.py").write_text("stashed = 1\n")
        git(ws, "stash", "-q")
    assert not ks.remove_workspace_dir(db, "t_11111111", ws, remote_host=HOST)
    assert (ws / "code.py").exists()


def test_remote_mutation_without_residue_branch_holds(db, tmp_path, monkeypatch):
    """Mutation: the remote proof with its residue test removed holds the residue fixture again."""
    script = ks._REMOTE_PROOF
    mutant = script.replace("r[:2] in('??','!!') and res(r[3:])", "False")
    assert mutant != script
    monkeypatch.setattr(ks, "_REMOTE_PROOF", mutant)
    ws = remote_ws(tmp_path)
    residue(ws)
    assert not ks.remove_workspace_dir(db, "t_11111111", ws, remote_host=HOST)
    assert ws.exists()


# ------------------------------------------------------------------ nested repo under a residue dir

def nested_repo(ws, tmp_path, unpushed):
    """A vendored checkout / editable install inside .venv: its own repo, optionally with work."""
    inner = ws / ".venv" / "src" / "vendored"
    make_repo(inner, tmp_path / f"inner-{unpushed}.git")
    if unpushed:
        (inner / "code.py").write_text("value = 2\n")
        git(inner, "commit", "-qam", "unpushed work")
    return inner


@pytest.mark.parametrize("unpushed", [True, False])
def test_local_residue_dir_holding_a_nested_repo_is_held(board, tmp_path, unpushed):
    tid, ws = local_card(board)
    residue(ws)
    nested_repo(ws, tmp_path, unpushed)
    assert not ks._residue_only(ws)
    kb.complete_task(board, tid)
    survivor = kb.latest_run(board, tid).metadata.get("survivor") or {}
    assert survivor.get("kind") != "ref", survivor


def test_local_residue_without_nested_repo_is_still_deleted(board):
    tid, ws = local_card(board)
    residue(ws)
    (ws / ".venv" / "src" / "vendored").mkdir(parents=True)
    (ws / ".venv" / "src" / "vendored" / "code.py").write_text("plain file, no .git\n")
    assert ks._residue_only(ws)
    assert ks.remove_workspace_dir(board, tid, ws)
    assert not ws.exists()


@pytest.mark.parametrize("ignored", [False, True])
def test_remote_residue_dir_holding_a_nested_repo_is_held(db, tmp_path, ignored):
    ws = remote_ws(tmp_path)
    residue(ws)
    if ignored:  # `!! .venv/` is reported collapsed; the walk must still find the nested .git
        (ws / ".git" / "info" / "exclude").write_text(".venv/\n")
    inner = nested_repo(ws, tmp_path, unpushed=True)
    assert not ks.remove_workspace_dir(db, "t_11111111", ws, remote_host=HOST)
    assert (inner / ".git").exists()


def test_remote_residue_without_nested_repo_is_deleted(db, tmp_path):
    ws = remote_ws(tmp_path)
    residue(ws)
    (ws / ".git" / "info" / "exclude").write_text(".venv/\n")
    (ws / ".venv" / "src" / "vendored").mkdir(parents=True)
    (ws / ".venv" / "src" / "vendored" / "code.py").write_text("plain file, no .git\n")
    assert ks.remove_workspace_dir(db, "t_11111111", ws, remote_host=HOST)
    assert not ws.exists()


# ------------------------------------------------------------------ follow-up safety regressions

@pytest.mark.parametrize("bare", [False, True])
def test_local_ignored_repository_beside_visible_residue_is_held(board, tmp_path, bare):
    tid, ws = local_card(board)
    residue(ws)
    (ws / ".git" / "info" / "exclude").write_text(".venv/\n")
    inner = ws / ".venv" / "src" / "hidden"
    if bare:
        git(ws, "clone", "-q", "--bare", str(ws), str(inner))
    else:
        nested_repo(ws, tmp_path, unpushed=True)
    assert not ks._residue_only(ws)


@pytest.mark.parametrize("ignored", [False, True])
def test_remote_bare_repository_under_residue_is_held(db, tmp_path, ignored):
    ws = remote_ws(tmp_path)
    residue(ws)
    if ignored:
        (ws / ".git" / "info" / "exclude").write_text(".venv/\n")
    inner = ws / ".venv" / "bare.git"
    git(ws, "clone", "-q", "--bare", str(ws), str(inner))
    assert not ks.remove_workspace_dir(db, "t_11111111", ws, remote_host=HOST)
    assert (inner / "HEAD").exists()


def test_remote_carriage_return_in_residue_path_cannot_hide_repository(db, tmp_path):
    ws = remote_ws(tmp_path)
    inner = ws / ".venv" / "vendor\rcheckout"
    make_repo(inner, tmp_path / "inner.git")
    (inner / "code.py").write_text("unpushed = 1\n")
    git(inner, "commit", "-qam", "unpushed")
    assert not ks.remove_workspace_dir(db, "t_11111111", ws, remote_host=HOST)
    assert (inner / ".git").exists()


# ------------------------------------------------------------------ pins

def _literal(script, name):
    m = re.search(r"^%s=(\(.*?\))$" % name, script, re.M)
    assert m, name
    return ast.literal_eval(m.group(1))


def test_remote_residue_list_matches_local():
    assert _literal(ks._REMOTE_PROOF, "R") == ks._RESIDUE_DIRS
    assert _literal(ks._REMOTE_PROOF, "F") == ks._RESIDUE_FILES
    assert set(ks._RESIDUE_DIRS) == {"__pycache__", ".pytest_cache", ".venv", "node_modules",
                                     ".mypy_cache", ".ruff_cache"}
    assert set(ks._RESIDUE_FILES) == {"*.pyc", ".coverage"}


def test_remote_proof_stays_small():
    # Shipped as one argv over ssh; the fleet-nas seccomp note caps it.
    assert len(ks._REMOTE_PROOF.encode()) < 4096
