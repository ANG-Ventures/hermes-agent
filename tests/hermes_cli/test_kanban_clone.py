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
    for key, value in kc.MIRROR_CONFIG:
        got = _git("--git-dir", str(mirror), "config", "--get-all", key).splitlines()
        assert value in got, key


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
    args = parser.parse_args(["kanban", "clone", "https://github.com/Kyzcreig/demo.git", "c", "-b", "main"])
    assert kanban.kanban_command(args) == 0
    assert (fleet / "c" / ".git" / "objects" / "info" / "alternates").exists()
