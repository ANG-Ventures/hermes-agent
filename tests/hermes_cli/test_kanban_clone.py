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
    # --filter is dropped when a mirror is in use: a partial clone ignores
    # --reference and would download every object the filter keeps.
    # Own-object count, not size-pack: --depth writes an empty (0-object) pack.
    stats = _git("count-objects", "-v", cwd=dest).splitlines()
    assert "count: 0" in stats and "in-pack: 0" in stats, stats
    assert subprocess.run(["git", "config", "--get", "remote.up.partialclonefilter"],
                          cwd=dest, capture_output=True).returncode == 1
    assert _git("config", "--get", "remote.up.fetch", cwd=dest) == "+refs/heads/main:refs/remotes/up/main"
    assert _git("config", "--get", "remote.up.tagopt", cwd=dest) == "--no-tags"


def test_filter_is_kept_when_there_is_no_mirror(fleet, monkeypatch, capsys):
    blocker = fleet / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv(kc.MIRRORS_ENV, str(blocker))
    assert _run_cli(["--filter=blob:none", "https://github.com/Kyzcreig/demo.git", "f"]) == 0
    assert _git("config", "--get", "remote.origin.partialclonefilter", cwd=fleet / "f") == "blob:none"
    assert "mirror unavailable" in capsys.readouterr().err


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


def test_failed_mirror_build_leaves_no_mirror_and_no_staging(fleet, monkeypatch):
    """An in-process failure is discarded, never left for the next caller."""
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
    owner_dir = fleet / "mirrors" / "Kyzcreig"
    mirror = owner_dir / "demo.git"
    assert not mirror.exists()
    assert not list(owner_dir.glob(".demo.git.build-*"))
    assert kc.ensure_mirror("https://github.com/Kyzcreig/demo.git", "Kyzcreig", "demo") == mirror
    assert (mirror / "objects").is_dir()
    assert not list(owner_dir.glob(".demo.git.*"))


def _residue_files(repo_dir):
    return sorted(p.name for p in Path(repo_dir).rglob("*")
                  if p.name.startswith("tmp_") or p.name.endswith(".lock"))


def _dead_build(owner_dir, name):
    """What a SIGKILLed first build leaves: a staging repo with the never-prune
    config, a half-written pack, and ref + config locks (QA r2 R1 b/c)."""
    staging = owner_dir / name
    _git("init", "-q", "--bare", str(staging))
    _git("--git-dir", str(staging), "config", "core.repositoryformatversion", "1")
    _git("--git-dir", str(staging), "config", "extensions.preciousObjects", "true")
    (staging / "objects" / "pack" / "tmp_pack_KILLED").write_bytes(b"\0" * 300_000)
    (staging / "objects" / "pack" / "tmp_idx_KILLED").write_bytes(b"\0" * 10)
    (staging / "refs" / "heads" / "main.lock").write_text("0" * 40 + "\n")
    (staging / "config.lock").write_text("")
    return staging


@pytest.mark.parametrize("name", [".demo.git.build-deadbeef", ".demo.git.partial"])
def test_dead_build_residue_is_discarded_not_promoted(fleet, name):
    owner_dir = fleet / "mirrors" / "Kyzcreig"
    owner_dir.mkdir(parents=True)
    dead = _dead_build(owner_dir, name)
    assert kc.clone("https://github.com/Kyzcreig/demo.git", "a") == 0
    mirror = owner_dir / "demo.git"
    assert (fleet / "a" / ".git" / "objects" / "info" / "alternates").read_text().strip() == str(
        mirror / "objects")
    assert _residue_files(mirror) == [], "killed build residue promoted into the mirror"
    assert not dead.exists()
    assert not list(owner_dir.glob(".demo.git.*"))


def test_discard_only_touches_this_repos_staging(fleet, tmp_path):
    owner_dir = fleet / "mirrors" / "Kyzcreig"
    owner_dir.mkdir(parents=True)
    keep = [owner_dir / "demo.git.build-x", owner_dir / ".other.git.build-x", owner_dir / "demo.git"]
    for k in keep:
        k.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("x")
    (owner_dir / ".demo.git.build-link").symlink_to(outside)
    # A symlink to a sibling INSIDE the owner dir passes the realpath check;
    # only the symlink guard keeps it from reaching the live mirror.
    (owner_dir / "demo.git" / "sentinel").write_text("live")
    (owner_dir / ".demo.git.build-inner").symlink_to(owner_dir / "demo.git")
    kc._discard_dead_builds(owner_dir, "demo")
    assert all(k.exists() for k in keep)
    assert (outside / "sentinel").exists()
    assert (owner_dir / "demo.git" / "sentinel").read_text() == "live"


_HELPER = (
    "import sys; sys.path.insert(0, sys.argv[1]);"
    "from hermes_cli import kanban_clone as kc;"
    "sys.exit(kc.clone(sys.argv[2], sys.argv[3], ['-q']))"
)


def _spawn(dest, url="https://github.com/Kyzcreig/demo.git"):
    import sys

    root = str(Path(kc.__file__).resolve().parents[1])
    return subprocess.Popen(
        [sys.executable, "-c", _HELPER, root, url, str(dest)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )


def _terminal_tool_kill(proc):
    """What tools/environments/local.py does on timeout: SIGTERM the group,
    wait up to 1 s, then SIGKILL it."""
    import os
    import signal
    import time

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.02)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.communicate()


def _dir_bytes(path):
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


@pytest.fixture
def big_fleet(fleet):
    """demo with ~24 MB of incompressible history and 1,500 branches, so a
    first build takes long enough to be killed mid-pack and mid-ref-update."""
    import os

    work = fleet / "src" / "work" / "Kyzcreig" / "demo"
    for i in range(3):
        (work / f"blob{i}.bin").write_bytes(os.urandom(8_000_000))
        _git("add", ".", cwd=work)
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"big {i}", cwd=work)
    head = _git("rev-parse", "HEAD", cwd=work)
    refs = "".join(f"create refs/heads/b{i:04d} {head}\n" for i in range(1500))
    subprocess.run(["git", "update-ref", "--stdin"], input=refs, text=True, check=True, cwd=work)
    bare = fleet / "src" / "Kyzcreig" / "demo.git"
    _git("push", "-q", "--force", str(bare), "refs/heads/*:refs/heads/*", cwd=work)
    return fleet


def test_killed_first_build_never_poisons_the_mirror(big_fleet):
    """QA r2 R1: kill REAL helper subprocesses the way the terminal tool does,
    at points spread across the whole build (pack transfer and ref update).
    After every kill the next call must build the mirror, clone with
    alternates, and promote nothing from the killed attempt."""
    import shutil
    import time

    owner_dir = big_fleet / "mirrors" / "Kyzcreig"
    mirror = owner_dir / "demo.git"

    # Clean reference build: its size and duration.
    t0 = time.monotonic()
    proc = _spawn(big_fleet / "clean")
    _, err = proc.communicate(timeout=300)
    build_s = time.monotonic() - t0
    assert proc.returncode == 0, err
    clean_bytes = _dir_bytes(mirror)
    shutil.move(str(mirror), str(big_fleet / "clean-mirror"))

    fractions = (0.05, 0.15, 0.3, 0.45, 0.6, 0.75, 0.85, 0.95)
    landed = 0
    for n, frac in enumerate(fractions):
        victim = _spawn(big_fleet / f"killed{n}")
        time.sleep(build_s * frac)
        _terminal_tool_kill(victim)
        if mirror.exists():  # the kill landed after promotion: nothing to prove
            shutil.move(str(mirror), str(big_fleet / f"late{n}"))
            continue
        landed += 1
        nxt = _spawn(big_fleet / f"next{n}")
        _, err = nxt.communicate(timeout=300)
        assert nxt.returncode == 0, err
        assert "mirror unavailable" not in err, (frac, err)
        alt = big_fleet / f"next{n}" / ".git" / "objects" / "info" / "alternates"
        assert alt.read_text().strip() == str(mirror / "objects"), frac
        assert _residue_files(mirror) == [], frac
        assert _dir_bytes(mirror) <= clean_bytes * 1.05, (frac, _dir_bytes(mirror), clean_bytes)
        assert not list(owner_dir.glob(".demo.git.*")), frac
        shutil.move(str(mirror), str(big_fleet / f"built{n}"))
    # Non-vacuity: most kills must land before promotion, or this proves nothing.
    assert landed >= len(fractions) // 2, (landed, build_s)


def test_concurrent_first_clones_share_one_mirror(fleet):
    """QA r2 K1/MX3: without the flock most concurrent first clones degrade."""
    procs = [_spawn(fleet / f"c{i}") for i in range(6)]
    outs = [p.communicate(timeout=120) for p in procs]
    mirror = fleet / "mirrors" / "Kyzcreig" / "demo.git"
    for i, (p, (_, err)) in enumerate(zip(procs, outs)):
        assert p.returncode == 0, err
        assert "mirror unavailable" not in err, err
        alt = fleet / f"c{i}" / ".git" / "objects" / "info" / "alternates"
        assert alt.read_text().strip() == str(mirror / "objects")
    assert not list((fleet / "mirrors" / "Kyzcreig").glob(".demo.git.*"))


@pytest.mark.parametrize("arm", ["index", "gitdir"])
def test_inherited_git_env_cannot_redirect_the_clone(fleet, monkeypatch, arm):
    """QA r2 K1/MX4: a caller inside another repo's git env must not have its
    index or config touched, and the clone must still borrow from the mirror."""
    victim = fleet / "victim"
    victim.mkdir()
    _git("init", "-q", "-b", "main", cwd=victim)
    (victim / "f").write_text("v\n")
    _git("add", "f", cwd=victim)
    before = ((victim / ".git" / "index").read_bytes(), (victim / ".git" / "config").read_bytes())
    monkeypatch.setenv("GIT_INDEX_FILE", str(victim / ".git" / "index"))
    if arm == "gitdir":
        monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(victim))
    assert kc.clone("https://github.com/Kyzcreig/demo.git", "a") == 0
    assert ((victim / ".git" / "index").read_bytes(), (victim / ".git" / "config").read_bytes()) == before
    assert (fleet / "a" / ".git" / "objects" / "info" / "alternates").exists()
    assert (fleet / "a" / "big.txt").exists()


def test_never_prune_config_is_in_place_before_the_fetch(fleet, monkeypatch):
    """QA r2 K2: no object may land in a repo that could still prune it."""
    real = kc._git
    seen = {}

    def spy(*args):
        if "fetch" in args:
            git_dir = args[args.index("--git-dir") + 1]
            for key in ("extensions.preciousObjects", "gc.pruneExpire"):
                seen[key] = subprocess.run(
                    ["git", "--git-dir", git_dir, "config", "--get", key],
                    capture_output=True, text=True).stdout.strip()
        return real(*args)

    monkeypatch.setattr(kc, "_git", spy)
    kc.ensure_mirror("https://github.com/Kyzcreig/demo.git", "Kyzcreig", "demo")
    assert seen == {"extensions.preciousObjects": "true", "gc.pruneExpire": "never"}


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


# --- local-path sources (card t_e122e9cd) ------------------------------------
# A plain ``git clone <path>`` hard-links the source's packs; on 2026-09-25 one
# ~/.hermes pack had 135 links and every git freshen of it emitted ~135 FSEvents.


def _pack_links(repo: Path) -> list:
    return [p.stat().st_nlink for p in (repo / ".git" / "objects" / "pack").glob("*.pack")]


def _local_repo(fleet, origin=None):
    work = fleet / "localsrc"
    _git("clone", "-q", "https://github.com/someone/other.git", str(work))
    _git("repack", "-qad", cwd=work)
    if origin:
        _git("remote", "set-url", "origin", origin, cwd=work)
    assert _pack_links(work) == [1], "fixture must start unshared"
    return work


def test_local_path_source_is_never_hard_linked(fleet):
    src = _local_repo(fleet)
    assert kc.clone(str(src), "h") == 0
    assert (fleet / "h" / "big.txt").read_text().startswith("payload")
    assert _pack_links(fleet / "h") and all(n == 1 for n in _pack_links(fleet / "h"))
    assert _pack_links(src) == [1], "source pack gained a hard link"
    assert not (fleet / "h" / ".git" / "objects" / "info" / "alternates").exists()


def test_plain_git_clone_of_a_local_path_does_hard_link(fleet):
    """Control for the test above: proves the fixture CAN hard-link, so a
    passing no-link assertion is the helper's doing, not the filesystem's."""
    src = _local_repo(fleet)
    _git("clone", "-q", str(src), "plain", cwd=fleet)
    assert _pack_links(src) == [2]


def test_local_source_of_a_fleet_repo_borrows_from_its_mirror(fleet):
    src = _local_repo(fleet, origin="https://github.com/Kyzcreig/demo.git")
    # an unpushed local commit must still arrive in the clone
    (src / "local.txt").write_text("only here\n")
    _git("add", ".", cwd=src)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local", cwd=src)
    assert kc.clone(str(src), "m") == 0
    mirror = fleet / "mirrors" / "Kyzcreig" / "demo.git"
    alternates = (fleet / "m" / ".git" / "objects" / "info" / "alternates").read_text()
    assert alternates.strip() == str(mirror / "objects")
    assert (fleet / "m" / "local.txt").read_text() == "only here\n"
    assert all(n == 1 for n in _pack_links(fleet / "m"))
    assert _pack_links(src) == [1]


def test_file_url_and_missing_path_are_not_local_sources(fleet):
    assert kc.local_source(f"file://{fleet}") is None
    assert kc.local_source(str(fleet / "does-not-exist")) is None
    assert kc.local_source("https://github.com/someone/other.git") is None
    assert kc.local_source(str(fleet)) == fleet
