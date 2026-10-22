"""``hermes kanban clone`` arms the fleet root's pre-push chain (card t_a8f5c76b).

10-18/19: four hermes-home PRs were pushed from kanban-clone scratch repos that
had no hooks (git clone copies none; only the fleet root sets core.hooksPath), so
scripts/prose-in-pr-prepush.py never ran and fleet-merge refused them rc 42 at land.

A fake fleet root holds remote.origin.url + scripts/git-hooks/pre-push; GitHub URLs
are rewritten onto local bare repos so real ``git push`` runs the installed hook.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_clone as kc

HOME_URL = "https://github.com/ANG-Ventures/hermes-home.git"
OTHER_URL = "https://github.com/Kyzcreig/demo.git"


def _git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True, capture_output=True)


@pytest.fixture
def root(tmp_path, monkeypatch):
    src = tmp_path / "src"
    for owner, repo in (("ANG-Ventures", "hermes-home"), ("Kyzcreig", "demo")):
        work = src / "work" / owner / repo
        work.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=work)
        (work / "a.txt").write_text("x\n")
        _git("add", ".", cwd=work)
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=work)
        bare = src / owner / (repo + ".git")
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "-q", "--bare", str(work), str(bare))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.file://{src}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv(kc.MIRRORS_ENV, str(tmp_path / "mirrors"))
    fleet = tmp_path / "fleet-root"
    (fleet / "scripts" / "git-hooks").mkdir(parents=True)
    _git("init", "-q", cwd=fleet)
    _git("config", "remote.origin.url", "git@github.com:ANG-Ventures/hermes-home.git", cwd=fleet)
    hook = fleet / "scripts" / "git-hooks" / "pre-push"
    # Stand-in for the real chain: record what it saw, refuse every push.
    hook.write_text('#!/usr/bin/env bash\ncat > "$(dirname "$0")/seen"\necho "$@" >> "$(dirname "$0")/seen"\n'
                    'echo "prose-in-pr: REFUSED (fixture)" >&2\nexit 1\n')
    hook.chmod(0o755)
    monkeypatch.setenv(kc.HOOKS_ROOT_ENV, str(fleet))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _commit_and_push(repo: Path, branch: str):
    _git("checkout", "-q", "-b", branch, cwd=repo)
    (repo / "b.txt").write_text("y\n")
    _git("add", ".", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c", cwd=repo)
    return _git("push", "-q", "origin", "HEAD", cwd=repo, check=False)


def test_clone_of_the_fleet_root_repo_runs_the_root_prepush_chain(root):
    assert kc.clone(HOME_URL, "home") == 0
    hook = root / "home" / ".git" / "hooks" / "pre-push"
    assert os.access(hook, os.X_OK) and kc.SHIM_MARKER in hook.read_text()
    r = _commit_and_push(root / "home", "daedalus/t_x-prose")
    assert r.returncode != 0, "push went through without the fleet chain"
    assert "REFUSED (fixture)" in r.stderr
    seen = (root / "fleet-root" / "scripts" / "git-hooks" / "seen").read_text()
    assert "refs/heads/daedalus/t_x-prose" in seen and "origin" in seen


def test_a_worktree_of_the_clone_shares_the_hook(root):
    """#4591 was pushed from a worktree (t_e20bd128/wt) of the kanban-clone repo."""
    assert kc.clone(HOME_URL, "home") == 0
    _git("worktree", "add", "-q", "-b", "wtb", str(root / "wt"), cwd=root / "home")
    (root / "wt" / "c.txt").write_text("z\n")
    _git("add", ".", cwd=root / "wt")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c", cwd=root / "wt")
    r = _git("push", "-q", "origin", "HEAD:refs/heads/wtb", cwd=root / "wt", check=False)
    assert r.returncode != 0 and "REFUSED (fixture)" in r.stderr


def test_other_repos_get_no_hook(root):
    assert kc.clone(OTHER_URL, "demo") == 0
    assert not (root / "demo" / ".git" / "hooks" / "pre-push").exists()
    assert _commit_and_push(root / "demo", "feature").returncode == 0


def test_missing_root_chain_at_push_time_refuses(root):
    assert kc.clone(HOME_URL, "home") == 0
    (root / "fleet-root" / "scripts" / "git-hooks" / "pre-push").unlink()
    r = _commit_and_push(root / "home", "b")
    assert r.returncode != 0 and "fleet pre-push chain cannot run" in r.stderr


def test_no_root_chain_at_clone_time_arms_nothing(root):
    (root / "fleet-root" / "scripts" / "git-hooks" / "pre-push").unlink()
    assert kc.clone(HOME_URL, "home") == 0
    assert not (root / "home" / ".git" / "hooks" / "pre-push").exists()


def test_existing_hookspath_or_prepush_is_left_alone(root):
    assert kc.clone(HOME_URL, "home") == 0
    hook = root / "home" / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    assert kc.arm_fleet_prepush(root / "home", HOME_URL) is None
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"
    hook.unlink()
    _git("config", "core.hooksPath", "/elsewhere", cwd=root / "home")
    assert kc.arm_fleet_prepush(root / "home", HOME_URL) is None
    assert not hook.exists()


@pytest.mark.parametrize("url", [
    "git@github.com:ANG-Ventures/hermes-home.git",
    "https://github.com/ang-ventures/hermes-home",
    "ssh://git@github.com/ANG-Ventures/hermes-home.git",
])
def test_root_repo_match_ignores_url_form(url):
    assert kc._same_repo(kc.parse_fleet_repo(url), kc.parse_fleet_repo(HOME_URL))
    assert not kc._same_repo(kc.parse_fleet_repo(url), kc.parse_fleet_repo(OTHER_URL))
