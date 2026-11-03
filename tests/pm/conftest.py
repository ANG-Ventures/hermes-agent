"""Shared fixtures for the PM suite."""
from pathlib import Path

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_machine_home: keep Path.home() intact (a subprocess or stage_runtime needs the real one)")


@pytest.fixture(autouse=True, scope="session")
def bounded_malloc_arenas():
    """Keep uv's glibc malloc arenas under the harness's 6 GiB RLIMIT_AS (tests/_fixtures/bounds.py).

    uv spawns one rayon thread per CPU and glibc reserves a 64 MiB arena per thread, so on a
    32-thread host (ace-media) `uv sync` dies ``memory allocation of 15 bytes failed`` (rc -6) under
    the address-space cap while the box has 70 GB free: 18 errors in test_source_update_launch.py on
    every main sweep there (deploy-green 6c5b8c1b62, 8b83c39559, 2026-11-02). Two arenas keep it
    under the cap; test_plugin_survival_contract.py set this per-test for the same reason, every PM
    test that reaches real uv needs it. Session scope on purpose: the first uv call is the
    module-scoped ``isolated_python`` fixture, which runs before any function-scoped monkeypatch.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MALLOC_ARENA_MAX", "2")
        yield


@pytest.fixture(autouse=True)
def isolated_machine_home(request, tmp_path, monkeypatch):
    """Machine-scoped PM state (store, caches, profiles root) lands under tmp_path.

    The root conftest already sandboxes HERMES_HOME; PM additionally keys its machine cache
    off the home directory, so that moves too — in this process (Path.home) and in any child
    (HOME / USERPROFILE). Opt out with ``@pytest.mark.real_machine_home``.
    """
    if request.node.get_closest_marker("real_machine_home"):
        return
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
