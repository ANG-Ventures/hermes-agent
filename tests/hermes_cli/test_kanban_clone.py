"""Tests for ``hermes kanban clone`` (card t_dad1edd7).

GitHub URLs are redirected to local ``file://`` repos with ``url.<x>.insteadOf``
(passed through GIT_CONFIG_COUNT env), so the real git transport runs end to
end with no network and no hardlink shortcut.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_clone as kc


def _git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A local 'Kyzcreig/demo' origin, a non-fleet 'someone/other' origin,
    and GitHub URLs for both rewritten onto them."""
    src = tmp_path / "src"
    for owner, repo in (("Kyzcreig", "demo"), ("someone", "other")):
        work = src / "work" / owner / repo
        work.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=work)
        (work / "big.txt").write_text("payload\n" * 50000)
        _git("add", ".", cwd=work)
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=work)
        bare = src / owner / (repo + ".git")
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "-q", "--bare", str(work), str(bare))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.file://{src}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv(kc.MIRRORS_ENV, str(tmp_path / "mirrors"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/ANG-Ventures/hermes-home.git", ("ANG-Ventures", "hermes-home")),
        ("https://github.com/ang-ventures/hermes-home", ("ANG-Ventures", "hermes-home")),
        ("git@github.com:Kyzcreig/hermes-agent.git", ("Kyzcreig", "hermes-agent")),
        ("ssh://git@github.com/Kyzcreig/fleet-ops-scripts", ("Kyzcreig", "fleet-ops-scripts")),
        ("https://x-access-token@github.com/Kyzcreig/hermes-agent.git/", ("Kyzcreig", "hermes-agent")),
        ("https://github.com/NousResearch/hermes-agent.git", None),
        ("https://gitlab.com/Kyzcreig/hermes-agent.git", None),
        ("https://github.com/Kyzcreig", None),
        ("", None),
    ],
)
def test_parse_fleet_repo(url, expected):
    assert kc.parse_fleet_repo(url) == expected


def test_default_dest():
    assert kc.default_dest("https://github.com/Kyzcreig/hermes-agent.git") == "hermes-agent"
    assert kc.default_dest("git@github.com:Kyzcreig/demo") == "demo"


def test_fleet_clone_borrows_objects_from_lazy_mirror(fleet):
    assert kc.clone("https://github.com/Kyzcreig/demo.git", "a") == 0

    mirror = fleet / "mirrors" / "Kyzcreig" / "demo.git"
    assert (mirror / "objects").is_dir(), "mirror not created lazily"
    alternates = (fleet / "a" / ".git" / "objects" / "info" / "alternates").read_text()
    assert alternates.strip() == str(mirror / "objects")
    # The checkout's own store holds none of the history.
    assert _git("count-objects", "-v", cwd=fleet / "a").splitlines()[1] == "size: 0"
    assert "size-pack: 0" in _git("count-objects", "-v", cwd=fleet / "a")
    assert (fleet / "a" / "big.txt").read_text().startswith("payload")

    # The never-prune invariant is written into the mirror at creation.
    # Literal values, NOT a loop over kc.MIRROR_CONFIG: a table the test
    # re-reads from the implementation cannot catch a key dropped from it.
    expected = {
        "core.repositoryformatversion": "1",
        "extensions.preciousobjects": "true",
        "gc.auto": "0",
        "gc.pruneexpire": "never",
        "gc.reflogexpire": "never",
        "gc.reflogexpireunreachable": "never",
        "maintenance.auto": "false",
        "fetch.prune": "false",
    }
    for key, value in expected.items():
        assert _git("--git-dir", str(mirror), "config", "--get", key) == value, key
    # A fully built mirror leaves no staging repo behind.
    assert not list((fleet / "mirrors" / "Kyzcreig").glob(".*.partial"))


def test_second_clone_reuses_mirror(fleet):
    assert kc.clone("https://github.com/Kyzcreig/demo.git", "a") == 0
    mirror = fleet / "mirrors" / "Kyzcreig" / "demo.git"
    marker = mirror / "reuse-marker"
    marker.write_text("x")
    assert kc.clone("git@github.com:Kyzcreig/demo.git".replace(
        "git@github.com:", "https://github.com/"), "b") == 0
    assert marker.exists(), "mirror was rebuilt instead of reused"
    assert (fleet / "b" / ".git" / "objects" / "info" / "alternates").exists()


def test_non_fleet_url_is_plain_clone(fleet):
    assert kc.clone("https://github.com/someone/other.git", "o") == 0
    assert not (fleet / "o" / ".git" / "objects" / "info" / "alternates").exists()
    assert not (fleet / "mirrors").exists()


def test_mirror_failure_degrades_to_plain_clone(fleet, monkeypatch, capsys):
    blocker = fleet / "not-a-dir"
    blocker.write_text("file where the mirrors root should be")
    monkeypatch.setenv(kc.MIRRORS_ENV, str(blocker))
    assert kc.clone("https://github.com/Kyzcreig/demo.git", "a") == 0
    assert (fleet / "a" / "big.txt").exists()
    assert not (fleet / "a" / ".git" / "objects" / "info" / "alternates").exists()
    assert "mirror unavailable" in capsys.readouterr().err


def test_cli_dispatch_skips_kanban_db(fleet, monkeypatch):
    import argparse

    from hermes_cli import kanban

    def _boom():
        raise AssertionError("clone must not touch kanban.db")

    monkeypatch.setattr(kanban.kb, "init_db", _boom)
    parser = argparse.ArgumentParser()
    kanban.build_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["kanban", "clone", "-b", "main", "https://github.com/Kyzcreig/demo.git", "c"])
    assert kanban.kanban_command(args) == 0
    assert (fleet / "c" / ".git" / "objects" / "info" / "alternates").exists()


def _run_cli(argv):
    import argparse

    from hermes_cli import kanban

    parser = argparse.ArgumentParser()
    kanban.build_parser(parser.add_subparsers(dest="cmd"))
    return kanban.kanban_command(parser.parse_args(["kanban", "clone", *argv]))


def test_cli_forwards_the_git_clone_options_workers_use(fleet):
    """QA r1 B5b: -q/--depth/--filter/--no-checkout/... were rc=2 before."""
    rc = _run_cli([
        "-q", "--depth", "1", "--filter=blob:none", "--no-checkout",
        "--single-branch", "--no-tags", "--origin", "up", "-b", "main",
        "https://github.com/Kyzcreig/demo.git", "d",
    ])
    assert rc == 0
    dest = fleet / "d"
    assert (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert (dest / ".git" / "shallow").exists(), "--depth not forwarded"
    assert not (dest / "big.txt").exists(), "--no-checkout not forwarded"
    assert _git("remote", cwd=dest) == "up", "--origin not forwarded"
    assert _git("config", "--get", "remote.up.partialclonefilter", cwd=dest) == "blob:none"
    assert _git("config", "--get", "remote.up.fetch", cwd=dest) == "+refs/heads/main:refs/remotes/up/main"
    assert _git("config", "--get", "remote.up.tagopt", cwd=dest) == "--no-tags"


def test_cli_accepts_double_dash_before_url(fleet):
    assert _run_cli(["-q", "--", "https://github.com/Kyzcreig/demo.git", "e"]) == 0
    assert (fleet / "e" / "big.txt").exists()


def test_mirror_root_is_fleet_root_under_a_profile_home(tmp_path, monkeypatch):
    """QA r1 A5: workers run with HERMES_HOME=<root>/profiles/<name>."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "daedalus").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "daedalus"))
    monkeypatch.delenv(kc.MIRRORS_ENV, raising=False)
    assert kc.mirrors_root() == root / "mirrors"
    assert kc.mirror_path("Kyzcreig", "demo") == root / "mirrors" / "Kyzcreig" / "demo.git"


def test_userinfo_is_not_persisted_in_the_shared_mirror(fleet, monkeypatch):
    """QA r1 C1: a token-bearing URL must not land in the mirror's config."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", f"url.file://{fleet / 'src'}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "https://someone:s3cret@github.com/")
    assert kc.clone("https://someone:s3cret@github.com/Kyzcreig/demo.git", "a") == 0
    mirror = fleet / "mirrors" / "Kyzcreig" / "demo.git"
    assert _git("--git-dir", str(mirror), "config", "--get", "remote.origin.url") == (
        "https://github.com/Kyzcreig/demo.git")
    assert "s3cret" not in (mirror / "config").read_text()


def test_failed_mirror_build_leaves_no_mirror_and_resumes(fleet, monkeypatch):
    """A half-built mirror is never visible; the next call finishes the build."""
    real = kc._git
    calls = {"n": 0}

    def flaky(*args):
        if "fetch" in args and calls["n"] == 0:
            calls["n"] += 1
            return subprocess.CompletedProcess(args, 1, "", "network down")
        return real(*args)

    monkeypatch.setattr(kc, "_git", flaky)
    with pytest.raises(RuntimeError, match="network down"):
        kc.ensure_mirror("https://github.com/Kyzcreig/demo.git", "Kyzcreig", "demo")
    mirror = fleet / "mirrors" / "Kyzcreig" / "demo.git"
    assert not mirror.exists()
    assert kc.ensure_mirror("https://github.com/Kyzcreig/demo.git", "Kyzcreig", "demo") == mirror
    assert (mirror / "objects").is_dir()
    assert not (fleet / "mirrors" / "Kyzcreig" / ".demo.git.partial").exists()


@pytest.mark.parametrize("precious", [True, False])
def test_explicit_prune_on_mirror_cannot_corrupt_a_dependent(fleet, precious):
    """QA r1 B3(c): `git gc --prune=now` overrides gc.pruneExpire=never.

    extensions.preciousObjects is what stops it. The precious=False arm is the
    control: with the extension removed the same operation DOES corrupt the
    dependent, so the True arm is measuring the guard, not a no-op.
    """
    assert kc.clone("https://github.com/Kyzcreig/demo.git", "a") == 0
    mirror = fleet / "mirrors" / "Kyzcreig" / "demo.git"
    dep = fleet / "a"
    assert "size-pack: 0" in _git("count-objects", "-v", cwd=dep)  # objects live in the mirror

    # Upstream force-pushes an unrelated history; a refresh makes the
    # dependent's commit unreachable inside the mirror.
    work = fleet / "src" / "work" / "Kyzcreig" / "demo"
    _git("checkout", "-q", "--orphan", "rewrite", cwd=work)
    (work / "big.txt").write_text("rewritten\n")
    _git("add", ".", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "rewrite", cwd=work)
    _git("push", "-q", "--force", str(fleet / "src" / "Kyzcreig" / "demo.git"), "HEAD:main", cwd=work)
    _git("--git-dir", str(mirror), "fetch", "-q", "origin")

    if not precious:
        _git("--git-dir", str(mirror), "config", "--unset", "extensions.preciousObjects")
    subprocess.run(["git", "--git-dir", str(mirror), "gc", "-q", "--prune=now"],
                   capture_output=True, text=True, check=False)

    fsck = subprocess.run(["git", "fsck", "--connectivity-only"], cwd=dep,
                          capture_output=True, text=True, check=False)
    head = subprocess.run(["git", "cat-file", "-e", "HEAD^{tree}"], cwd=dep,
                          capture_output=True, text=True, check=False)
    if precious:
        assert fsck.returncode == 0, fsck.stderr
        assert head.returncode == 0
    else:
        assert fsck.returncode != 0 or head.returncode != 0, "control arm did not corrupt"
