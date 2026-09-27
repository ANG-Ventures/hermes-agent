"""t_26209188: remote survivor checks must not inherit the scratch tripwire.

Scratch workspaces sit under a gitfile pointing at
``/nonexistent/kanban-scratch-workspace-is-not-a-repo`` (incident t_82a5c853).
That is correct for LOCAL git. But git's repository discovery reads the gitfile
even for ``git ls-remote <url>``, so the remote verifier -- which ran in the
worker's cwd -- exited 128 before contacting the remote, and
``--survivor-ref`` could never verify from a scratch card (reproduced on
t_2cdabecb, runs 8687 and 9402).

Measured: the gitfile in the cwd ancestry is what fails (alone, or with
``GIT_WORK_TREE``); ``GIT_DIR`` set to a missing path does NOT fail
``ls-remote`` (it skips discovery). The verifier now scrubs repo-locating env
and runs from a neutral cwd, so every shape is covered.

These tests use real git against a real remote tag -- no subprocess mocks.
"""
import os
import subprocess

import pytest

from hermes_cli import kanban_external_survivor as ext

TRIPWIRE = "/nonexistent/kanban-scratch-workspace-is-not-a-repo"
TASK = "t_26209188"
FAKE_URL = "https://example.invalid/survivor-probe.git"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


# Env a worker may carry on top of the gitfile. The first two are the incident
# (rc 128 on unfixed code); the GIT_DIR shape guards the env scrub.
SHAPES = {
    "gitfile": {},
    "gitfile+work_tree": {"GIT_WORK_TREE": "{ws}"},
    "gitfile+git_dir": {"GIT_DIR": TRIPWIRE, "GIT_WORK_TREE": "{ws}"},
}
INCIDENT_SHAPES = ("gitfile", "gitfile+work_tree")


@pytest.fixture(params=list(SHAPES))
def tripwired_scratch(request, tmp_path, monkeypatch):
    """A worker standing in a scratch workspace under the live tripwire shape."""
    workspaces = tmp_path / "workspaces"
    ws = workspaces / TASK
    ws.mkdir(parents=True)
    (workspaces / ".git").write_text(f"gitdir: {TRIPWIRE} (repo tripwire)\n")
    monkeypatch.chdir(ws)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in SHAPES[request.param].items():
        monkeypatch.setenv(key, value.format(ws=ws))
    # Precondition: local git from here refuses with 128 -- the tripwire works.
    probe = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True)
    assert probe.returncode == 128
    return request.param


@pytest.fixture
def remote_with_tag(tmp_path, monkeypatch):
    """A real bare remote carrying ``refs/tags/survivor/<task>``.

    ``_safe_url`` rightly refuses ``file://`` claims, so the claim names an
    https URL and a global ``insteadOf`` routes git to the local bare repo.
    Git itself resolves the URL, which is exactly the path under test.
    """
    src = tmp_path / "src"
    src.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, env=env)
    for args in (("config", "user.name", "Test"), ("config", "user.email", "t@example.invalid"),
                 ("commit", "-q", "--allow-empty", "-m", "survivor"),
                 ("tag", f"survivor/{TASK}")):
        subprocess.run(["git", "-C", str(src), *args], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], check=True, env=env,
                         capture_output=True).stdout.decode().strip()
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True, env=env)

    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[url "file://{bare}"]\n\tinsteadOf = {FAKE_URL}\n'
                         '[protocol "file"]\n\tallow = always\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return sha


def test_ls_remote_from_tripwired_cwd_is_the_incident(tripwired_scratch, remote_with_tag):
    """Without neutralising cwd/env, remote git dies on the tripwire (rc 128)."""
    if tripwired_scratch not in INCIDENT_SHAPES:
        pytest.skip("GIT_DIR bypasses discovery; ls-remote tolerates a missing GIT_DIR")
    raw = subprocess.run(["git", "ls-remote", "--tags", "--", FAKE_URL], capture_output=True)
    assert raw.returncode == 128


def test_survivor_ref_verifies_from_tripwired_scratch(tripwired_scratch, remote_with_tag):
    verified = ext.verify_ref(f"{FAKE_URL}#{remote_with_tag}", mined_for=TASK)
    assert verified is not None
    assert verified["sha"] == remote_with_tag
    assert verified["branch"] == f"refs/tags/survivor/{TASK}"
    assert verified["corroborated_by"] == "branch"


def test_tripwire_still_blocks_local_git_after_remote_query(tripwired_scratch, remote_with_tag):
    """The fix scrubs only the child process; the worker's tripwire is intact."""
    before = dict(os.environ)
    ext.verify_ref(f"{FAKE_URL}#{remote_with_tag}", mined_for=TASK)
    assert dict(os.environ) == before
    assert subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode == 128


def test_survivor_ref_unknown_sha_is_a_verdict_not_unavailable(tripwired_scratch, remote_with_tag):
    """The remote answers; a SHA it does not carry is a verdict, not RemoteUnavailable.

    Since #962 the verdict for a non-tip SHA on a non-github remote is a
    fail-closed ``Unverified`` (ancestry unprovable) rather than ``None``;
    either way the remote was reached, which is what this test pins.
    """
    with pytest.raises(ext.Unverified):
        ext.verify_ref(f"{FAKE_URL}#{'0' * 40}", mined_for=TASK)
