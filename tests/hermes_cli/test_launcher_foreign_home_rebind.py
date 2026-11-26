"""A process with a test/scratch HERMES_HOME must not rebind the install's launchers (t_968b018f).

ACE-AI 10-18 17:56: a pytest run with HERMES_HOME=tmp_path/home shelled out to the real
`hermes`; the launcher self-heal resolved the store under the TEST home and rewrote the
checkout's shared .hermes/bin/hermes to exec tmp_path/.../python3. Once pytest pruned
tmp_path, every `hermes` on the host exited 127 for ~7 h.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from hermes_cli import _launchers

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell launchers")


def _store(home: Path, entry: str = "python-fixture") -> Path:
    """A PM store under ``home`` whose python tool is a link to this interpreter."""
    store = home / "tools"
    python = store / entry / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys._base_executable).resolve())
    (store / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"python": {
        "version": "fixture", "entry": entry}}}), encoding="utf-8")
    return python


@pytest.fixture
def install(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    real_home = tmp_path / ".hermes"
    real_python = _store(real_home)
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    local = repo / ".hermes" / "bin"
    written = _launchers.ensure_install_launchers(repo, local)
    assert len(written) == len(_launchers.ENTRY_POINTS)
    return repo, local, real_python


def _bytes(local: Path) -> dict:
    return {name: (local / name).read_bytes() for name in _launchers.ENTRY_POINTS}


def test_test_home_does_not_rebind_install_launchers(install, tmp_path, monkeypatch):
    repo, local, real_python = install
    before = _bytes(local)
    assert str(real_python).encode() in before["hermes"]
    test_home = tmp_path / "pytest-of-ace" / "test_real_cli_mints_once_and_c0" / "home"
    test_python = _store(test_home)
    monkeypatch.setenv("HERMES_HOME", str(test_home))
    assert _launchers.resolve_store_python(repo) == test_python
    written = _launchers.ensure_install_launchers(repo, local)
    assert len(written) == len(_launchers.ENTRY_POINTS)
    assert _bytes(local) == before
    assert str(test_python).encode() not in (local / "hermes").read_bytes()


def test_repin_inside_the_same_store_still_republishes(install, tmp_path, monkeypatch):
    repo, local, real_python = install
    store = real_python.parents[2]
    newer = store / "python-newer" / "bin" / "python3"
    newer.parent.mkdir(parents=True)
    newer.symlink_to(Path(sys._base_executable).resolve())
    (store / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"python": {
        "version": "newer", "entry": "python-newer"}}}), encoding="utf-8")
    _launchers.ensure_install_launchers(repo, local)
    assert str(newer).encode() in (local / "hermes").read_bytes()


def test_dead_interpreter_is_still_healed_from_another_store(install, tmp_path, monkeypatch):
    repo, local, real_python = install
    real_python.unlink()
    other = _store(tmp_path / "elsewhere")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
    _launchers.ensure_install_launchers(repo, local)
    assert str(other).encode() in (local / "hermes").read_bytes()


def test_launcher_bound_to_another_real_home_is_kept(install, tmp_path, monkeypatch):
    repo, local, _ = install
    before = _bytes(local)
    other_home = tmp_path / "home-of-bob" / ".hermes"
    _store(other_home)
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before


@pytest.fixture
def persistent(tmp_path, monkeypatch):
    """Checkout outside every temp root; HOME and its store stay in pytest's tmp (a /tmp HOME)."""
    source = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix=".launcher-regression-", dir=source) as directory:
        root = Path(directory)
        repo = root / "checkout"
        repo.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        yield root, repo, repo / ".hermes" / "bin"


def _select(monkeypatch, home: Path) -> None:
    """Pin the store, so resolving it never walks up past the checkout for an install stamp."""
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(home / "tools"))


def test_working_launcher_is_never_rebound_to_another_store(persistent, tmp_path, monkeypatch):
    """Store A's launcher works, so store B leaves it byte-identical, wherever A lives."""
    root, repo, local = persistent
    local.mkdir(parents=True)
    store_a = _store(tmp_path / "home-a" / ".hermes")
    for name in _launchers.ENTRY_POINTS:
        _launchers.mint_launcher(name, repo, local, store_a, None)
    before = _bytes(local)
    _store(root / "home-b" / ".hermes")
    _select(monkeypatch, root / "home-b" / ".hermes")
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before


def test_broken_launcher_is_repaired_under_a_tmp_home(persistent, tmp_path, monkeypatch):
    """Prism 7138eebbd730: HOME under the temp root, Python repinned and the old one GC'd."""
    _, repo, local = persistent
    local.mkdir(parents=True)
    home = tmp_path / ".hermes"
    python = _store(home, "python-new")
    old = home / "tools" / "python-old" / "bin" / "python3"
    for name in _launchers.ENTRY_POINTS:
        _launchers.mint_launcher(name, repo, local, old, None)
    _select(monkeypatch, home)
    written = _launchers.ensure_install_launchers(repo, local)
    assert len(written) == len(_launchers.ENTRY_POINTS)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == python


def test_fresh_install_under_a_tmp_home_gets_launchers(persistent, tmp_path, monkeypatch):
    """Prism 7138eebbd730: a container with HOME=/tmp still publishes its commands."""
    _, repo, local = persistent
    home = tmp_path / ".hermes"
    python = _store(home)
    _select(monkeypatch, home)
    written = _launchers.ensure_install_launchers(repo, local)
    assert len(written) == len(_launchers.ENTRY_POINTS)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == python
        assert os.access(local / name, os.X_OK)


def test_tmpdir_home_repairs_dead_launcher_in_fresh_process():
    """An environment-selected temp root is not evidence that a store is disposable."""
    # Outside pytest/scratch roots so the real classifier sees persistent paths.
    source = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix=".launcher-regression-", dir=source) as directory:
        root = Path(directory)
        home = root / "user"
        home.mkdir()
        repo = root / "checkout"
        repo.mkdir()
        (repo / "hermes_constants.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def get_default_hermes_root(): return Path(os.environ['HERMES_HOME'])\n")
        (repo / "hermes_bootstrap.py").write_text("")
        (repo / "launcher_probe.py").write_text("print('fixture-launcher-ok')\n")
        python = _store(home / ".hermes", "python-new")
        env = {**os.environ, "HOME": str(home), "TMPDIR": str(home),
               "HERMES_HOME": str(home / ".hermes")}
        env.pop("HERMES_RUNTIME_DIR", None)
        script = dedent("""\
            import sys
            import tempfile
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            from hermes_cli import _launchers
            repo, python = map(Path, sys.argv[2:])
            assert Path(tempfile.gettempdir()) == Path.home()
            local = repo / '.hermes' / 'bin'
            local.mkdir(parents=True)
            old = Path.home() / '.hermes' / 'tools' / 'python-old' / 'bin' / 'python3'
            for name in _launchers.ENTRY_POINTS:
                _launchers.mint_launcher(name, repo, local, old, None)
            assert not old.exists()
            _launchers.ensure_install_launchers(repo, local)
            for name in _launchers.ENTRY_POINTS:
                assert _launchers._shell_launcher_python(local / name) == python
            """)
        repaired = subprocess.run(
            [sys.executable, "-I", "-c", script, str(source), str(repo), str(python)],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        assert repaired.returncode == 0, repaired.stdout + repaired.stderr
        launched = subprocess.run(
            [str(repo / ".hermes" / "bin" / "hermes"), "--run-module", "launcher_probe"],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        assert launched.returncode == 0, launched.stdout + launched.stderr
        assert launched.stdout.strip() == "fixture-launcher-ok"


def _stand_in(tmp_path: Path, real_home: Path, monkeypatch) -> Path:
    """A private home whose tools/ links to the real store (ci-box 11-25, t_926efa60)."""
    stand_in = tmp_path / "stand-in" / "wh"
    stand_in.mkdir(parents=True)
    (stand_in / "tools").symlink_to(real_home / "tools")
    monkeypatch.setenv("HERMES_HOME", str(stand_in))
    return stand_in


def test_stand_in_home_linked_to_the_real_store_leaves_launchers_untouched(install, tmp_path, monkeypatch):
    repo, local, real_python = install
    before = _bytes(local)
    stamps = {name: (local / name).stat().st_mtime_ns for name in _launchers.ENTRY_POINTS}
    inodes = {name: (local / name).stat().st_ino for name in _launchers.ENTRY_POINTS}
    stand_in = _stand_in(tmp_path, real_python.parents[3], monkeypatch)
    assert str(_launchers.resolve_store_python(repo)).startswith(str(stand_in))
    written = _launchers.ensure_install_launchers(repo, local)
    assert len(written) == len(_launchers.ENTRY_POINTS)
    assert _bytes(local) == before
    assert {name: (local / name).stat().st_mtime_ns for name in _launchers.ENTRY_POINTS} == stamps
    assert {name: (local / name).stat().st_ino for name in _launchers.ENTRY_POINTS} == inodes
    (stand_in.parent / "wh" / "tools").unlink()
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._launcher_works(local / name)


def test_repin_seen_through_a_stand_in_home_keeps_the_real_spelling(install, tmp_path, monkeypatch):
    repo, local, real_python = install
    store = real_python.parents[2]
    newer = store / "python-newer" / "bin" / "python3"
    newer.parent.mkdir(parents=True)
    newer.symlink_to(Path(sys._base_executable).resolve())
    (store / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"python": {
        "version": "newer", "entry": "python-newer"}}}), encoding="utf-8")
    stand_in = _stand_in(tmp_path, real_python.parents[3], monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == newer
        assert str(stand_in).encode() not in (local / name).read_bytes()


def test_missing_launcher_target_is_still_repaired_from_a_normal_run(install, tmp_path, monkeypatch):
    repo, local, real_python = install
    for name in _launchers.ENTRY_POINTS:
        _launchers.mint_launcher(name, repo, local, real_python.parents[2] / "gone" / "bin" / "python3", None)
        assert not _launchers._launcher_works(local / name)
    _launchers.ensure_install_launchers(repo, local)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == real_python
        assert _launchers._launcher_works(local / name)


def test_real_home_publish_moves_a_stand_in_launcher_back_to_the_direct_path(install, tmp_path, monkeypatch):
    repo, local, real_python = install
    real_home = real_python.parents[3]
    for name in _launchers.ENTRY_POINTS:
        (local / name).unlink()
    stand_in = _stand_in(tmp_path, real_home, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    for name in _launchers.ENTRY_POINTS:
        assert str(stand_in).encode() not in (local / name).read_bytes()  # minted resolved
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    _launchers.ensure_install_launchers(repo, local)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == real_python
        assert str(stand_in).encode() not in (local / name).read_bytes()
    (stand_in / "tools").unlink()
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._launcher_works(local / name)


def test_stand_in_cannot_respell_a_real_home_that_sits_behind_symlinks(tmp_path, monkeypatch):
    # Prism 7d225792d1cb: /home -> /mnt/users and /home/w/.hermes -> /srv/hermes-home;
    # a stand-in whose tools/ names the real spelling must not take the shared launchers.
    (tmp_path / "mnt" / "users" / "w").mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "mnt" / "users")
    (tmp_path / "srv" / "hermes-home").mkdir(parents=True)
    (tmp_path / "home" / "w" / ".hermes").symlink_to(tmp_path / "srv" / "hermes-home")
    real_home = tmp_path / "home" / "w" / ".hermes"
    real_python = _store(real_home)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home" / "w"))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    local = repo / ".hermes" / "bin"
    _launchers.ensure_install_launchers(repo, local)
    before = _bytes(local)
    stamps = {name: (local / name).stat().st_mtime_ns for name in _launchers.ENTRY_POINTS}
    assert str(real_python).encode() in before["hermes"]
    stand_in = _stand_in(tmp_path, real_home, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before
    assert {name: (local / name).stat().st_mtime_ns for name in _launchers.ENTRY_POINTS} == stamps
    (stand_in / "tools").unlink()
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._launcher_works(local / name)


def test_dotdot_link_target_is_followed_not_collapsed(tmp_path, monkeypatch):
    # Prism ff1151e22154: ~/.hermes -> alias/../hermes with alias -> durable/anchor resolves to
    # durable/hermes; a lexical collapse reads it as ~/hermes, where a stand-in may sit.
    w = tmp_path / "w"
    (tmp_path / "durable" / "anchor").mkdir(parents=True)
    (tmp_path / "durable" / "hermes").mkdir()
    w.mkdir()
    (w / "alias").symlink_to(tmp_path / "durable" / "anchor")
    (w / ".hermes").symlink_to("alias/../hermes")
    real_home = w / ".hermes"
    real_python = _store(real_home)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(w))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    local = repo / ".hermes" / "bin"
    _launchers.ensure_install_launchers(repo, local)
    before = _bytes(local)
    assert str(real_python).encode() in before["hermes"]
    stand_in = w / "hermes"
    stand_in.mkdir()
    (stand_in / "tools").symlink_to(real_home / "tools")
    monkeypatch.setenv("HERMES_HOME", str(stand_in))
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before
    (stand_in / "tools").unlink()
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._launcher_works(local / name)


# Operator cut (t_926efa60, 11-25 19:15): the direction is chosen by store ownership, not link
# topology. The real home sits behind two links (home -> mnt/users, .hermes -> data/hermes);
# a stand-in's tools/ names the real store in one of four spellings.
_SPELLINGS = ("literal", "half-resolved", "resolved", "relative-dotdot")


def _linked_real_home(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, Path]:
    (tmp_path / "mnt" / "users" / "w").mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "mnt" / "users")
    (tmp_path / "data" / "hermes").mkdir(parents=True)
    (tmp_path / "mnt" / "users" / "w" / ".hermes").symlink_to(tmp_path / "data" / "hermes")
    real_home = tmp_path / "home" / "w" / ".hermes"
    real_python = _store(real_home)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home" / "w"))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    return repo, repo / ".hermes" / "bin", real_home, real_python


def _spelled_stand_in(tmp_path: Path, spelling: str, monkeypatch, name: str = "wh") -> Path:
    stand_in = tmp_path / "scratch" / name
    stand_in.mkdir(parents=True)
    target = {
        "literal": tmp_path / "home" / "w" / ".hermes" / "tools",
        "half-resolved": tmp_path / "mnt" / "users" / "w" / ".hermes" / "tools",
        "resolved": tmp_path / "data" / "hermes" / "tools",
        "relative-dotdot": Path("..", "..", "data", "hermes", "tools"),
    }[spelling]
    (stand_in / "tools").symlink_to(target)
    monkeypatch.setenv("HERMES_HOME", str(stand_in))
    return stand_in


def _all_work(local: Path) -> bool:
    return all(_launchers._launcher_works(local / name) for name in _launchers.ENTRY_POINTS)


@pytest.mark.parametrize("spelling", _SPELLINGS)
def test_real_home_behind_links_moves_a_stand_in_minted_launcher_back(tmp_path, monkeypatch, spelling):
    # Independent read on 16f0c6d8: the head left these on the stand-in path; base moved them back.
    repo, local, real_home, real_python = _linked_real_home(tmp_path, monkeypatch)
    stand_in = _spelled_stand_in(tmp_path, spelling, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)  # missing -> minted through the resolved store
    assert all(str(stand_in).encode() not in (local / n).read_bytes() for n in _launchers.ENTRY_POINTS)
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    _launchers.ensure_install_launchers(repo, local)
    assert all(str(stand_in).encode() not in (local / n).read_bytes() for n in _launchers.ENTRY_POINTS)
    stand_in.rename(stand_in.with_name("gone"))
    assert _all_work(local)


@pytest.mark.parametrize("spelling", _SPELLINGS)
def test_stand_in_leaves_working_launchers_byte_identical(tmp_path, monkeypatch, spelling):
    repo, local, real_home, real_python = _linked_real_home(tmp_path, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    before = _bytes(local)
    stamps = {name: (local / name).stat().st_mtime_ns for name in _launchers.ENTRY_POINTS}
    stand_in = _spelled_stand_in(tmp_path, spelling, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before
    assert {name: (local / name).stat().st_mtime_ns for name in _launchers.ENTRY_POINTS} == stamps
    stand_in.rename(stand_in.with_name("gone"))
    assert _all_work(local)


def test_publish_order_stand_in_real_stand_in_ends_unchanged(tmp_path, monkeypatch):
    repo, local, real_home, real_python = _linked_real_home(tmp_path, monkeypatch)
    stand_in = _spelled_stand_in(tmp_path, "resolved", monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    _launchers.ensure_install_launchers(repo, local)
    after_real = _bytes(local)
    monkeypatch.setenv("HERMES_HOME", str(stand_in))
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == after_real
    stand_in.rename(stand_in.with_name("gone"))
    assert _all_work(local)


def test_real_home_under_a_second_symlinked_spelling_moves_launchers_back(tmp_path, monkeypatch):
    repo, local, real_home, real_python = _linked_real_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))  # the second spelling is not under ~/.hermes
    (tmp_path / "alias").symlink_to(tmp_path / "data")
    second = tmp_path / "alias" / "hermes"
    stand_in = _spelled_stand_in(tmp_path, "literal", monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    monkeypatch.setenv("HERMES_HOME", str(second))
    _launchers.ensure_install_launchers(repo, local)
    for name in _launchers.ENTRY_POINTS:
        assert str(second).encode() in (local / name).read_bytes()
    stand_in.rename(stand_in.with_name("gone"))
    assert _all_work(local)


def test_chained_stand_ins_leave_working_launchers_unchanged(tmp_path, monkeypatch):
    repo, local, real_home, real_python = _linked_real_home(tmp_path, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    before = _bytes(local)
    first = _spelled_stand_in(tmp_path, "literal", monkeypatch, name="first")
    second = tmp_path / "scratch" / "second"
    second.mkdir()
    (second / "tools").symlink_to(first / "tools")
    monkeypatch.setenv("HERMES_HOME", str(second))
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before


def test_dotdot_inside_a_stand_in_link_leaves_launchers_unchanged(tmp_path, monkeypatch):
    repo, local, real_home, real_python = _linked_real_home(tmp_path, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    before = _bytes(local)
    stand_in = tmp_path / "scratch" / "wh"
    stand_in.mkdir(parents=True)
    (stand_in / "tools").symlink_to(tmp_path / "scratch" / ".." / "home" / "w" / ".hermes" / "tools")
    monkeypatch.setenv("HERMES_HOME", str(stand_in))
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before


def test_stand_in_link_loop_stops_without_raising(tmp_path, monkeypatch):
    repo, local, real_home, real_python = _linked_real_home(tmp_path, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    before = _bytes(local)
    stand_in = tmp_path / "scratch" / "wh"
    stand_in.mkdir(parents=True)
    (stand_in / "tools").symlink_to(stand_in / "loop")
    (stand_in / "loop").symlink_to(stand_in / "tools")
    monkeypatch.setenv("HERMES_HOME", str(stand_in))
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before


def test_launcher_whose_old_path_no_longer_resolves_is_repaired_then_moved_back(tmp_path, monkeypatch):
    repo, local, real_home, real_python = _linked_real_home(tmp_path, monkeypatch)
    for name in _launchers.ENTRY_POINTS:
        _launchers.mint_launcher(name, repo, local, tmp_path / "old-home" / "tools" / "python-x" / "bin" / "python3", None)
    assert not any(_launchers._launcher_works(local / n) for n in _launchers.ENTRY_POINTS)
    stand_in = _spelled_stand_in(tmp_path, "half-resolved", monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    assert _all_work(local)
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    _launchers.ensure_install_launchers(repo, local)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == real_python
    stand_in.rename(stand_in.with_name("gone"))
    assert _all_work(local)


# Operator item (t_926efa60, 11-25 19:55): a borrower never writes its own spelling. A missing
# launcher minted by a stand-in names the RESOLVED store, so it survives the stand-in going away
# with no move-back needed, whether the real home's tools/ is a real directory or a symlink.
def test_stand_in_minted_launcher_survives_the_stand_in_real_tools_dir(install, tmp_path, monkeypatch):
    repo, local, real_python = install
    for name in _launchers.ENTRY_POINTS:
        (local / name).unlink()
    stand_in = _stand_in(tmp_path, real_python.parents[3], monkeypatch)
    _launchers.ensure_install_launchers(repo, local)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == real_python.parents[2].resolve() / real_python.relative_to(real_python.parents[2])
    stand_in.rename(stand_in.with_name("gone"))
    assert _all_work(local)


def test_stand_in_minted_launcher_survives_the_stand_in_real_tools_symlink(tmp_path, monkeypatch):
    # The reader's probe on 31d52db8: the real home's own tools/ is a symlink, so the real home
    # "borrows" too and keeps a working launcher; the stand-in must not have minted its spelling.
    durable = tmp_path / "durable"
    real_python = _store(durable)  # durable/tools/python-fixture/bin/python3
    real_home = tmp_path / ".hermes"
    real_home.mkdir()
    (real_home / "tools").symlink_to(durable / "tools")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    local = repo / ".hermes" / "bin"
    stand_in = _stand_in(tmp_path, real_home, monkeypatch)
    _launchers.ensure_install_launchers(repo, local)  # missing -> minted
    for name in _launchers.ENTRY_POINTS:
        assert str(stand_in).encode() not in (local / name).read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(real_home))
    _launchers.ensure_install_launchers(repo, local)  # working launcher kept
    stand_in.rename(stand_in.with_name("gone"))
    assert _all_work(local)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == real_python
