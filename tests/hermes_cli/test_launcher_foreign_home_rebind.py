"""A process with a test/scratch HERMES_HOME must not rebind the install's launchers (t_968b018f).

ACE-AI 10-18 17:56: a pytest run with HERMES_HOME=tmp_path/home shelled out to the real
`hermes`; the launcher self-heal resolved the store under the TEST home and rewrote the
checkout's shared .hermes/bin/hermes to exec tmp_path/.../python3. Once pytest pruned
tmp_path, every `hermes` on the host exited 127 for ~7 h.
"""
import json
import os
import sys
from pathlib import Path

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
