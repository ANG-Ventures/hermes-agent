"""Language servers are lazy, reaped when idle, and capped per host (``lsp.max_servers_per_host``).

Each ``LSPService`` stands in for one agent process (a kanban worker); the cap is shared across
them through flock'd slot files in the per-test gateway lock dir.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.lsp import eventlog, host_slots
from agent.lsp.manager import LSPService
from agent.lsp.servers import SERVERS, ServerDef, SpawnSpec

MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


@pytest.fixture
def repo(monkeypatch, tmp_path):
    """A git workspace whose ``.py`` files route to a mock server; ``spawns`` counts process starts."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("")
    (root / "x.py").write_text("print('hi')\n")
    monkeypatch.chdir(str(root))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    eventlog.reset_announce_caches()
    spawns = []

    def _spawn(ws: str, ctx) -> SpawnSpec:
        spawns.append(ws)
        return SpawnSpec(command=[sys.executable, MOCK_SERVER], workspace_root=ws, cwd=ws,
                         env={"MOCK_LSP_SCRIPT": "errors"}, initialization_options={})

    index = next(i for i, s in enumerate(SERVERS) if s.server_id == "pyright")
    original = SERVERS[index]
    SERVERS[index] = ServerDef(
        server_id="pyright", extensions=original.extensions, resolve_root=lambda fp, ws: ws,
        build_spawn=_spawn, description="mock pyright")
    yield SimpleNamespace(path=root, spawns=spawns)
    SERVERS[index] = original


def _service(**kw) -> LSPService:
    return LSPService(enabled=True, wait_mode="document", wait_timeout=3.0, install_strategy="manual", **kw)


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_no_server_until_first_edit(repo):
    svc = _service(max_servers_per_host=6)
    try:
        f = str(repo.path / "x.py")
        assert svc.enabled_for(f)  # the pre-write gate must not spawn either
        assert repo.spawns == [] and host_slots.held_count(6) == 0
        svc.snapshot_baseline(f)  # what write_file/patch call before the first write
        assert len(repo.spawns) == 1 and host_slots.held_count(6) == 1
    finally:
        svc.shutdown()
    assert host_slots.held_count(6) == 0


def test_idle_server_is_stopped_and_frees_its_slot(repo):
    svc = _service(max_servers_per_host=6, idle_timeout=0.5)
    try:
        svc.snapshot_baseline(str(repo.path / "x.py"))
        assert svc.get_status()["clients"] and host_slots.held_count(6) == 1
        assert _wait_until(lambda: not svc.get_status()["clients"]), "idle server was never reaped"
        assert host_slots.held_count(6) == 0
    finally:
        svc.shutdown()


def test_cap_refuses_the_seventh_server_until_a_slot_frees(repo, caplog):
    f = str(repo.path / "x.py")
    workers = [_service(max_servers_per_host=6) for _ in range(7)]
    try:
        for svc in workers[:6]:
            svc.snapshot_baseline(f)
        assert len(repo.spawns) == 6 and host_slots.held_count(6) == 6

        seventh = workers[6]
        with caplog.at_level(logging.INFO, logger="hermes.lint.lsp"):
            assert not seventh.enabled_for(f)  # shell linter runs instead
            seventh.snapshot_baseline(f)
            assert seventh.get_diagnostics_sync(f) == []
        assert len(repo.spawns) == 6
        cap_lines = [r for r in caplog.records if "max_servers_per_host" in r.getMessage() and r.levelno == logging.INFO]
        assert len(cap_lines) == 1
        assert seventh.get_status()["broken"] == []  # over-cap is not broken: it retries later

        workers[0].shutdown()
        assert seventh.enabled_for(f)
        seventh.snapshot_baseline(f)
        assert len(repo.spawns) == 7 and host_slots.held_count(6) == 6
    finally:
        for svc in workers:
            svc.shutdown()
    assert host_slots.held_count(6) == 0
