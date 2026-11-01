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


@pytest.fixture
def ephemeral(tmp_path, monkeypatch):
    """The whole fixture lives in pytest's tmp; only ``<tmp_path>/tmp`` counts as a temp root."""
    root = (tmp_path / "tmp").resolve()
    monkeypatch.setattr(_launchers, "_is_ephemeral_path",
                        lambda p: Path(p).resolve().is_relative_to(root))
    return root


def test_real_install_rebinds_a_launcher_bound_to_a_live_tmp_store(
        install, ephemeral, tmp_path, monkeypatch):
    """Prism 57e9a766ab86: the 10-19 shape, while the tmp interpreter still exists."""
    repo, local, real_python = install
    test_python = _store(ephemeral / "pytest-of-ace" / "home")
    for name in _launchers.ENTRY_POINTS:
        _launchers.mint_launcher(name, repo, local, test_python, None)
    assert _launchers._shell_launcher_python(local / "hermes") == test_python
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _launchers.ensure_install_launchers(repo, local)
    for name in _launchers.ENTRY_POINTS:
        assert _launchers._shell_launcher_python(local / name) == real_python


def test_tmp_home_leaves_the_shared_launcher_byte_identical(install, ephemeral, monkeypatch):
    """Even with the bound interpreter gone, a test home never writes the shared file."""
    repo, local, real_python = install
    before = _bytes(local)
    real_python.unlink()
    test_home = ephemeral / "pytest-of-ace" / "home"
    _store(test_home)
    monkeypatch.setenv("HERMES_HOME", str(test_home))
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before


def test_tmp_home_reports_a_dead_shared_launcher_as_not_published(
        install, ephemeral, tmp_path, monkeypatch):
    """Prism 265fc36efda1: a dead shared launcher is not success, so healthy PATH commands stay."""
    repo, local, real_python = install
    healthy_python = _store(tmp_path / "other-live")
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    for name in _launchers.ENTRY_POINTS:
        _launchers.mint_launcher(name, repo, path_bin, healthy_python, None)
    healthy = _bytes(path_bin)
    real_python.unlink()
    test_home = ephemeral / "pytest-of-ace" / "home"
    _store(test_home)
    monkeypatch.setenv("HERMES_HOME", str(test_home))
    assert _launchers.stage_launcher("hermes", repo, local) is None
    assert _launchers.ensure_install_launchers(repo, path_bin) == []
    assert _bytes(path_bin) == healthy


def test_tmp_home_can_publish_its_own_isolated_launcher(install, ephemeral, monkeypatch):
    repo, local, _ = install
    before = _bytes(local)
    test_home = ephemeral / "home"
    test_python = _store(test_home)
    monkeypatch.setenv("HERMES_HOME", str(test_home))
    isolated = ephemeral / "bin"
    isolated.mkdir(parents=True)

    for name in _launchers.ENTRY_POINTS:
        target = _launchers.stage_launcher(name, repo, isolated)
        assert target == isolated / name
        assert _launchers._shell_launcher_python(target) == test_python
        assert os.access(target, os.X_OK)
    assert _bytes(local) == before


def test_launcher_bound_to_another_real_home_is_kept(install, ephemeral, tmp_path, monkeypatch):
    repo, local, _ = install
    before = _bytes(local)
    other_home = tmp_path / "home-of-bob" / ".hermes"
    _store(other_home)
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    _launchers.ensure_install_launchers(repo, local)
    assert _bytes(local) == before


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


@pytest.mark.parametrize("path, expected", [
    ("/tmp/pytest-of-ace/x/home/tools", True),
    ("/var/tmp/hermes-pytest/b-1/home/tools", True),
    ("/Users/ace/.hermes/profiles/coder/cache/scratch/h/tools", True),
    ("/home/bob/.hermes/tools", False),
])
def test_ephemeral_classifier(path, expected):
    assert _launchers._is_ephemeral_path(Path(path)) is expected
