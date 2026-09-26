"""A shared ``dir`` workspace too large to walk must still complete on its own work.

Card t_67f7a89c (follow-up to t_10e7c7ad). With other cards' nested repos
pruned, ``dir:~/.hermes`` still walked 94,906 directories (60 s cold) and
found one repository -- the root -- so every such card got "repository
enumeration exceeded the 50000 directory budget" instead of completing.

Contract pinned here, for the completion pass (``cleanup=False``) of a ``dir``
workspace only:
  * exhausting the walk budget is not a refusal; the repositories captured are
    what the walk found, the root, and every repository covering the card's
    ``changed_files`` or recorded ``bases``;
  * a claimed path inside a nested repo still refuses (nested recovery);
  * ``cleanup=True`` and non-``dir`` workspaces still refuse on the budget.

Mutation check: make ``_dir_repos`` re-raise -> the ``*_completes`` tests go
red; make ``_covering_repos`` return [] -> the claimed-nested, non-repo-root
and recorded-bases tests go red.
"""
import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as survivor


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
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


def _filler(ws, n=12):
    """Enough plain directories that a budget of 2 cannot reach anything nested."""
    for i in range(n):
        (ws / f"filler{i:02d}" / "inner").mkdir(parents=True)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


@pytest.fixture
def tiny_budget(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", "2")


@pytest.fixture
def home(tmp_path):
    """A shared home repo, too big for the budget; the card's work is a dirty file."""
    ws = _init(tmp_path / "home")
    _filler(ws)
    (ws / "code.py").write_text("value = 2\n")
    return ws


def _card(conn, ws, *, kind="dir"):
    tid = kb.create_task(conn, title="dir card", assignee="daedalus-opus",
                         workspace_kind=kind, workspace_path=str(ws))
    # Every fixture repo is younger than the card's birth: no predates-skip.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(time.time()) - 3600, tid))
    return tid


def _captured(result):
    manifest = json.loads(Path(result["sidecar"]).read_text())
    return {entry["repository"] for entry in manifest.get("repositories", [])}


def test_fixture_really_exceeds_the_budget(home, tiny_budget):
    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        survivor._repos(home)
    assert "directory budget" in str(caught.value)


def test_over_budget_dir_root_claim_completes(board, home, tiny_budget):
    tid = _card(board, home)

    result = survivor.preserve(board, tid, {"changed_files": ["code.py"]}, workspace=home)

    assert result["kind"] in {"bundle", "patch"} and _captured(result) == {"."}, result
    assert survivor._state(board, tid)[1] is None


def test_over_budget_claim_inside_nested_repo_still_refuses(board, home, tiny_budget):
    tid = _card(board, home)
    nested = _init(home / "filler05" / "inner" / "clone")
    (nested / "code.py").write_text("value = 3\n")

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        survivor.preserve(board, tid, {"changed_files": ["filler05/inner/clone/code.py"]},
                          workspace=home)
    assert "nested repository requires separate recovery" in str(caught.value)


def test_over_budget_non_repo_root_captures_claimed_repo_completes(board, tmp_path, tiny_budget):
    ws = tmp_path / "plain"
    _filler(ws)
    repo = _init(ws / "filler07" / "inner" / "work")
    (repo / "code.py").write_text("value = 4\n")
    tid = _card(board, ws)

    result = survivor.preserve(board, tid, {"changed_files": ["filler07/inner/work/code.py"]},
                               workspace=ws)

    assert _captured(result) == {"filler07/inner/work"}, result


def test_over_budget_recorded_base_is_not_reported_missing_completes(board, tmp_path, monkeypatch):
    ws = tmp_path / "plain"
    _filler(ws)
    repo = _init(ws / "filler03" / "inner" / "work")
    tid = _card(board, ws)
    survivor.record_baseline(board, tid, ws)  # default budget: sees the repo
    assert "filler03/inner/work" in survivor._state(board, tid)[0]
    (repo / "code.py").write_text("value = 5\n")
    monkeypatch.setenv("HERMES_KANBAN_SURVIVOR_WALK_BUDGET", "2")

    result = survivor.preserve(board, tid, {}, workspace=ws)

    assert _captured(result) == {"filler03/inner/work"}, result


def test_over_budget_symlinked_claim_is_not_followed(board, home, tmp_path, tiny_budget):
    outside = _init(tmp_path / "outside")
    (outside / "code.py").write_text("value = 6\n")
    (home / "link").symlink_to(outside, target_is_directory=True)
    tid = _card(board, home)

    result = survivor.preserve(board, tid, {"changed_files": ["code.py", "link/code.py"]},
                               workspace=home)

    assert _captured(result) == {"."}, result


def test_reclamation_pass_still_refuses_on_budget(board, home, tiny_budget):
    tid = _card(board, home)

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        survivor.preserve(board, tid, {"changed_files": ["code.py"]}, workspace=home, cleanup=True)
    assert "directory budget" in str(caught.value)


def test_worktree_workspace_still_refuses_on_budget(board, home, tiny_budget):
    tid = _card(board, home, kind="worktree")

    with pytest.raises(survivor.SurvivorUnavailable) as caught:
        survivor.preserve(board, tid, {"changed_files": ["code.py"]}, workspace=home)
    assert "directory budget" in str(caught.value)
