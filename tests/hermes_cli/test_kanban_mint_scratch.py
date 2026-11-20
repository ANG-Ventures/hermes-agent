"""A worktree card on a foreign-origin checkout mints scratch + Repo/Branch lines (t_efd1497a)."""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_mint_scratch as kms
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import kanban_home  # noqa: F401

UPSTREAM = "git@github.com:NousResearch/hermes-agent.git"
FORK = "https://github.com/ANG-Ventures/hermes-agent.git"


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def studio_agent(tmp_path):
    """The Studio's ~/.hermes/hermes-agent shape: origin fetches NousResearch, pushes ANG-Ventures."""
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", UPSTREAM, cwd=repo)
    _git("remote", "set-url", "--push", "origin", FORK, cwd=repo)
    kwt._origin_cache.clear()
    return repo


def _row(conn, tid):
    return conn.execute("SELECT workspace_kind, workspace_path, branch_name, body FROM tasks WHERE id=?",
                        (tid,)).fetchone()


def test_foreign_origin_worktree_mints_scratch_with_repo_and_branch(kanban_home, studio_agent, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rework", assignee="alpha", body="host: studio\nfix the thing",
                             workspace_kind="worktree", workspace_path=str(studio_agent),
                             branch_name="daedalus/t_ca2c9470-lcm-interior-stamp")
        kind, path, branch, body = _row(conn, tid)
    assert kind == "scratch"
    assert path != str(studio_agent) and ".worktrees" not in (path or "")
    assert branch is None
    assert "\nRepo: ANG-Ventures/hermes-agent\n" in body
    assert "\nBranch: daedalus/t_ca2c9470-lcm-interior-stamp" in body
    assert "fix the thing" in body
    assert kms.NOTICE_PREFIX + f"worktree:{studio_agent}" in capsys.readouterr().err


def test_existing_repo_line_is_kept_not_doubled(kanban_home, studio_agent):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rework", assignee="alpha", body="Repo: ANG-Ventures/hermes-agent\nx",
                             workspace_kind="worktree", workspace_path=str(studio_agent))
        kind, _path, _branch, body = _row(conn, tid)
    assert kind == "scratch"
    assert body.lower().count("repo:") == 1
    assert "Branch:" not in body


def test_own_origin_worktree_is_minted_as_asked(kanban_home, studio_agent):
    _git("remote", "set-url", "origin", FORK, cwd=studio_agent)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="wt", assignee="alpha", body="x",
                             workspace_kind="worktree", workspace_path=str(studio_agent))
        kind, path, _b, body = _row(conn, tid)
    assert (kind, path) == ("worktree", str(studio_agent))
    assert "Repo:" not in body


def test_foreign_push_url_is_minted_as_asked(kanban_home, studio_agent):
    """Nothing fleet-owned to name: the card keeps its worktree (the porter names origin_not_ours as before)."""
    _git("remote", "set-url", "--push", "origin", UPSTREAM, cwd=studio_agent)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="wt", assignee="alpha", body="x",
                             workspace_kind="worktree", workspace_path=str(studio_agent))
        assert _row(conn, tid)[0] == "worktree"


def test_not_a_checkout_and_scratch_are_untouched(tmp_path):
    assert kms.rewrite("worktree", str(tmp_path), None, "x") is None
    assert kms.rewrite("scratch", None, None, "x") is None
    assert kms.rewrite("worktree", "relative/path", None, "x") is None


def test_probe_fault_mints_as_asked(studio_agent):
    def boom(*a, **k):
        raise OSError("git missing")
    assert kms.rewrite("worktree", str(studio_agent), None, "x", runner=boom) is None


def test_conflicting_branch_line_keeps_the_worktree(kanban_home, studio_agent):
    """Prism 06ea8a9299c1: a body naming another branch must not silently replace the requested one."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rework", assignee="alpha", body="Branch: repair/old\nFix the issue",
                             workspace_kind="worktree", workspace_path=str(studio_agent), branch_name="repair/new")
        kind, path, branch, body = _row(conn, tid)
    assert (kind, path, branch) == ("worktree", str(studio_agent), "repair/new")
    assert "repair/new" not in body


def test_matching_branch_line_is_kept_not_doubled(kanban_home, studio_agent):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rework", assignee="alpha", body="Branch: `repair/new`\nFix it",
                             workspace_kind="worktree", workspace_path=str(studio_agent), branch_name="repair/new")
        kind, _path, branch, body = _row(conn, tid)
    assert (kind, branch) == ("scratch", None)
    assert body.lower().count("branch:") == 1


def _commit(repo, name):
    (repo / name).write_text(name)
    _git("add", name, cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name, cwd=repo)


def test_branch_with_local_only_commits_keeps_the_worktree(kanban_home, studio_agent):
    """Prism 00766c74ea02: an unpushed commit on the requested branch is not reachable from a fork clone."""
    _commit(studio_agent, "base")
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=studio_agent)
    _git("checkout", "-q", "-b", "feature", cwd=studio_agent)
    _commit(studio_agent, "unpushed")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rework", assignee="alpha", body="x",
                             workspace_kind="worktree", workspace_path=str(studio_agent), branch_name="feature")
        assert _row(conn, tid)[:3] == ("worktree", str(studio_agent), "feature")
    # once the push repo's tracking ref holds the tip (it was pushed to the fork), the card mints scratch
    _git("remote", "add", "fork", FORK, cwd=studio_agent)
    _git("update-ref", "refs/remotes/fork/feature", "feature", cwd=studio_agent)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rework", assignee="alpha", body="x",
                             workspace_kind="worktree", workspace_path=str(studio_agent), branch_name="feature")
        assert _row(conn, tid)[:3] == ("scratch", None, None)


CONTRIBUTOR = "https://github.com/Kyzcreig/hermes-agent.git"


def test_tip_held_only_by_another_remote_keeps_the_worktree(kanban_home, studio_agent):
    """Prism 06ea8a9299c1 (Wrong Remote): a tip only the upstream or a contributor fork holds is not on the fork
    the scratch clone uses, so the card keeps its worktree."""
    _commit(studio_agent, "base")
    _git("checkout", "-q", "-b", "feature", cwd=studio_agent)
    _commit(studio_agent, "theirs")
    _git("remote", "add", "contributor", CONTRIBUTOR, cwd=studio_agent)
    _git("update-ref", "refs/remotes/contributor/feature", "feature", cwd=studio_agent)
    _git("update-ref", "refs/remotes/origin/feature", "feature", cwd=studio_agent)
    _git("remote", "add", "fork", FORK, cwd=studio_agent)
    assert kms.rewrite("worktree", str(studio_agent), "feature", "x") is None
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rework", assignee="alpha", body="x",
                             workspace_kind="worktree", workspace_path=str(studio_agent), branch_name="feature")
        assert _row(conn, tid)[:3] == ("worktree", str(studio_agent), "feature")
