"""A language server that dies on its own is respawned with a growing backoff and its crash is logged once.

t_c0873696: the gateway's pyright V8-aborted ~5 min after every spawn; each respawn re-walked the home tree.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.lsp import eventlog
from agent.lsp.manager import CRASH_BACKOFF_SECONDS, LSPService
from agent.lsp.servers import SERVERS, ServerDef, SpawnSpec

MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="SIGABRT models the Node heap abort on POSIX")


@pytest.fixture
def repo(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "x.py").write_text("print('hi')\n", encoding="utf-8")
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
    yield SimpleNamespace(path=root, file=str(root / "x.py"), spawns=spawns)
    SERVERS[index] = original


def _service(**kw) -> LSPService:
    kw.setdefault("idle_timeout", 0)  # no background reaper: tests drive _reap_idle_once themselves
    return LSPService(enabled=True, wait_mode="document", wait_timeout=3.0, install_strategy="manual", **kw)


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _abort_server(svc: LSPService):
    """SIGABRT the running server, as V8 does on FatalProcessOutOfMemory."""
    [client] = list(svc._clients.values())
    os.kill(client.pid, signal.SIGABRT)
    assert _wait_until(lambda: client.crashed), "mock server did not die"
    return client


def _crash_lines(caplog, expect: int = None):
    """The WARNING crash lines; with ``expect``, wait for that many (the line is logged off-thread once
    the dead server's exit status is reaped)."""
    def lines():
        return [r.getMessage() for r in caplog.records
                if "died unexpectedly" in r.getMessage() and r.levelno == logging.WARNING]
    if expect is not None:
        _wait_until(lambda: len(lines()) >= expect, timeout=5.0)
        time.sleep(0.2)  # a duplicate would land in this window
    return lines()


def test_default_backoff_is_one_five_fifteen_minutes():
    assert CRASH_BACKOFF_SECONDS == (60.0, 300.0, 900.0)
    svc = LSPService(enabled=False, wait_mode="document", wait_timeout=1.0, install_strategy="manual")
    assert svc._crash_backoff == CRASH_BACKOFF_SECONDS


def test_crashed_server_is_not_respawned_until_its_backoff_passes(repo, caplog):
    svc = _service(crash_backoff_seconds=(1.0, 2.0))
    try:
        with caplog.at_level(logging.INFO, logger="hermes.lint.lsp"):
            svc.snapshot_baseline(repo.file)
            assert len(repo.spawns) == 1
            _abort_server(svc)

            # The next edits inside the backoff spawn nothing.
            assert not svc.enabled_for(repo.file)
            svc.snapshot_baseline(repo.file)
            assert svc.get_diagnostics_sync(repo.file) == []
            assert len(repo.spawns) == 1
            assert ("pyright", str(repo.path)) in svc.get_status()["broken"]
            lines = _crash_lines(caplog, expect=1)
            assert len(lines) == 1, lines  # logged once, not once per skipped edit
            assert "signal 6" in lines[0] and "crash 1 in a row" in lines[0] and "respawn in 1s" in lines[0]

            # After the backoff the pair gets a fresh server.
            assert _wait_until(lambda: svc.enabled_for(repo.file), timeout=5.0)
            svc.snapshot_baseline(repo.file)
            assert len(repo.spawns) == 2

            # A second crash in a row waits the next, longer step.
            _abort_server(svc)
            svc.snapshot_baseline(repo.file)
            assert len(repo.spawns) == 2
            lines = _crash_lines(caplog, expect=2)
            assert len(lines) == 2 and "crash 2 in a row" in lines[1] and "respawn in 2s" in lines[1]
    finally:
        svc.shutdown()


def test_reaper_logs_and_backs_off_a_crash_with_no_edit_in_between(repo, caplog):
    """The 01:25 crash on 11-19 sat unnoticed until the idle reaper reported it as a healthy 'reaped idle'."""
    svc = _service(crash_backoff_seconds=(60.0,))
    try:
        with caplog.at_level(logging.INFO, logger="hermes.lint.lsp"):
            svc.snapshot_baseline(repo.file)
            _abort_server(svc)
            svc._loop.run(svc._reap_idle_once(), timeout=5.0)  # last use is recent: not idle, but dead

            assert svc.get_status()["clients"] == []
            assert not any("reaped" in r.getMessage() for r in caplog.records)
            assert len(_crash_lines(caplog, expect=1)) == 1
            assert not svc.enabled_for(repo.file)
            svc.snapshot_baseline(repo.file)
            assert len(repo.spawns) == 1
            assert len(_crash_lines(caplog, expect=1)) == 1
    finally:
        svc.shutdown()


def test_healthy_idle_reap_resets_the_crash_count(repo, caplog):
    svc = _service(crash_backoff_seconds=(0.2, 60.0))
    try:
        with caplog.at_level(logging.INFO, logger="hermes.lint.lsp"):
            svc.snapshot_baseline(repo.file)
            _abort_server(svc)
            svc.snapshot_baseline(repo.file)
            assert len(repo.spawns) == 1
            assert _wait_until(lambda: svc.enabled_for(repo.file), timeout=5.0)
            svc.snapshot_baseline(repo.file)
            assert len(repo.spawns) == 2

            svc._idle_timeout = -1.0  # every client is idle now
            svc._loop.run(svc._reap_idle_once(), timeout=5.0)
            assert svc.get_status()["clients"] == []

            svc.snapshot_baseline(repo.file)
            assert len(repo.spawns) == 3
            _abort_server(svc)
            svc.snapshot_baseline(repo.file)
            lines = _crash_lines(caplog, expect=2)
            assert len(lines) == 2 and "crash 1 in a row" in lines[1], lines
    finally:
        svc.shutdown()


def test_server_stopped_by_the_service_is_not_a_crash(repo, caplog):
    svc = _service(crash_backoff_seconds=(60.0,))
    try:
        with caplog.at_level(logging.INFO, logger="hermes.lint.lsp"):
            svc.snapshot_baseline(repo.file)
            assert svc.release_workspace(str(repo.path)) == 1
            assert svc.enabled_for(repo.file)
            svc.snapshot_baseline(repo.file)
            assert len(repo.spawns) == 2
            assert _crash_lines(caplog, expect=0) == []
    finally:
        svc.shutdown()
