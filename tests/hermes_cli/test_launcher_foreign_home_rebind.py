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
