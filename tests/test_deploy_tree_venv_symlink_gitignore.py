"""The tracked .gitignore must ignore the deploy tree's venv family as SYMLINKS.

A deploy host's checkout does not contain a real ``venv`` directory: the atomic
venv flip makes ``venv`` and ``venv.prev`` SYMLINKS pointing into a
``venv.rel-*`` release directory. A gitignore pattern with a trailing slash
(``/venv/``) matches directories ONLY — it does not match a symlink, even one
that resolves to a directory. With the trailing-slash form those symlinks were
untracked, ``git status --porcelain`` was non-empty, and the deploy script's
clean-tree gate refused every deploy on that host for ~48h.

These tests therefore create SYMLINKS, not directories. A test that only makes a
real directory named ``venv`` passes against the buggy pattern and proves
nothing — that is exactly the gap that let the bug ship.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# One release dir plus the two symlinks deploy.sh maintains, and a *future*
# release timestamp so a later adoption stays ignored without another edit.
RELEASE_DIR = "venv.rel-adopted-20260922065309"
FUTURE_RELEASE_DIR = "venv.rel-99999999999999"
VENV_SYMLINKS = ("venv", "venv.prev")


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def deploy_tree(tmp_path: Path) -> Path:
    """A real git repo shaped like a deploy host's checkout after a venv flip."""
    repo = tmp_path / "deploy-tree-checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    shutil.copyfile(REPO_ROOT / ".gitignore", repo / ".gitignore")
    _run_git(repo, "add", ".gitignore")
    _run_git(
        repo,
        "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-qm", "init",
    )
    for rel in (RELEASE_DIR, FUTURE_RELEASE_DIR):
        release = repo / rel
        release.mkdir()
        (release / "bin").mkdir()
        (release / "bin" / "hermes").write_text("#!/bin/sh\n")
    for link in VENV_SYMLINKS:
        (repo / link).symlink_to(repo / RELEASE_DIR)
    return repo


@pytest.mark.parametrize("name", [*VENV_SYMLINKS, RELEASE_DIR, FUTURE_RELEASE_DIR])
def test_venv_family_is_ignored(deploy_tree: Path, name: str):
    """`git check-ignore` must match each venv-family entry by name.

    ``venv``/``venv.prev`` are symlinks here; a trailing-slash pattern returns
    exit 1 (no match) for them.
    """
    assert (deploy_tree / name).is_symlink() or (deploy_tree / name).is_dir()
    result = subprocess.run(
        ["git", "-C", str(deploy_tree), "check-ignore", "-v", "--", name],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{name} is NOT ignored (rc={result.returncode})"


def test_deploy_tree_reads_clean_with_the_venv_symlinks_present(deploy_tree: Path):
    """The gate deploy.sh actually runs: a non-empty porcelain refuses the deploy."""
    status = _run_git(deploy_tree, "status", "--porcelain", "--untracked-files=all")
    assert status.stdout == "", status.stdout


def test_venv_patterns_are_anchored_to_the_repo_root(deploy_tree: Path):
    """The leading `/` must keep the patterns root-only.

    A nested venv (a feature worktree's own, or a vendored one under a package)
    is a different decision and must not be swept up by this block.
    """
    nested = deploy_tree / "pkg"
    nested.mkdir()
    (nested / "venv").symlink_to(deploy_tree / RELEASE_DIR)
    result = subprocess.run(
        ["git", "-C", str(deploy_tree), "check-ignore", "-v", "--", "pkg/venv"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, f"pkg/venv unexpectedly ignored: {result.stdout}"
