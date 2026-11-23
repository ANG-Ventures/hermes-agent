"""SIGTERM at a console-session loss pages "session lost"; the shutdown diag lists processes without ps.

2026-11-22 21:23:35 PT launchd SIGTERM'd all 9 gateways when the console session was torn down: every
diag block's ps listing was empty and the home-channel notice read like a routine restart (t_06816049).
These drive the real SIGTERM handler (``_start_gateway_make_shutdown_signal_handler``) and the real
notice path (``_notify_active_sessions_of_shutdown``) with a fake console owner and a fake restart record.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

import gateway.run as run_mod
import gateway.shutdown_forensics as forensics
import gateway.status as status
import hermes_constants
from gateway.config import HomeChannel, Platform
from tests.gateway.restart_test_helpers import make_restart_runner

pytestmark = pytest.mark.platforms("linux", "macos")

OWN_UID = os.getuid() if hasattr(os, "getuid") else 0
ROOT_UID = 0 if OWN_UID != 0 else 4242  # "someone else" owns the console

# Stand-in for the fleet pager: notify.py appends a row to <root>/state/notify-sends/<day>.jsonl.
_FAKE_PAGER = """\
import json, sys, time, pathlib
a = sys.argv[1:]
row = {"producer": a[a.index("--producer") + 1], "severity": a[a.index("--severity") + 1],
       "via": a[a.index("--via") + 1], "cmd": a[0], "text": a[a.index("--text") + 1]}
d = pathlib.Path(__file__).resolve().parents[2] / "state" / "notify-sends"
d.mkdir(parents=True, exist_ok=True)
with open(d / (time.strftime("%Y-%m-%d") + ".jsonl"), "a") as f:
    f.write(json.dumps(row) + "\\n")
"""


@pytest.fixture
def fleet_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / "lib" / "episode_pager.py").write_text(_FAKE_PAGER, encoding="utf-8")
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda **_k: root)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(run_mod, "_hermes_home", home)
    monkeypatch.setattr(status, "consume_takeover_marker_for_self", lambda: False)
    monkeypatch.setattr(status, "consume_planned_stop_marker_for_self", lambda: False)
    monkeypatch.setattr(run_mod.asyncio, "create_task", lambda coro: coro.close())
    return root, home


def _ledger_rows(root: Path, wait_s: float = 0.0) -> list:
    deadline = time.monotonic() + wait_s
    while True:
        rows = []
        for f in sorted((root / "state" / "notify-sends").glob("*.jsonl")):
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        if rows or time.monotonic() >= deadline:
            return rows
        time.sleep(0.05)


def _sigterm(monkeypatch, runner, *, console_at_start: int, console_now: int) -> None:
    owner = [console_at_start]
    monkeypatch.setattr(forensics, "console_owner_uid", lambda: owner[0])
    handler = run_mod._start_gateway_make_shutdown_signal_handler(runner, [False])
    owner[0] = console_now  # the session dies between gateway start and the SIGTERM
    handler(signal.SIGTERM)


def _home_channel(runner) -> None:
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="home-chat", name="Home")


@pytest.mark.asyncio
async def test_sigterm_after_console_session_loss_pages_and_notice_says_so(monkeypatch, fleet_root):
    root, _home = fleet_root
    runner, adapter = make_restart_runner()
    _home_channel(runner)

    _sigterm(monkeypatch, runner, console_at_start=OWN_UID, console_now=ROOT_UID)

    rows = _ledger_rows(root, wait_s=10.0)
    assert len(rows) == 1, rows
    assert rows[0]["producer"] == forensics.SESSION_LOST_PRODUCER
    assert rows[0]["severity"] == "critical" and rows[0]["via"] == "fleet-alert" and rows[0]["cmd"] == "page"
    assert rows[0]["text"].splitlines()[0] == forensics.SESSION_LOST_FIRST_LINE

    await runner._notify_active_sessions_of_shutdown()
    assert len(adapter.sent_calls) == 1
    _chat, message, _meta = adapter.sent_calls[0]
    assert message.splitlines()[0] == (
        "console session lost: every service of this user is being stopped; not a planned restart")


@pytest.mark.asyncio
async def test_planned_restart_keeps_todays_notice_and_does_not_page(monkeypatch, fleet_root):
    root, _home = fleet_root
    runner, adapter = make_restart_runner()
    _home_channel(runner)
    runner._restart_requested = True  # the restart tool's record (request_restart)
    runner._restart_requester = "restart tool"

    _sigterm(monkeypatch, runner, console_at_start=OWN_UID, console_now=ROOT_UID)

    time.sleep(0.5)
    assert _ledger_rows(root) == []
    await runner._notify_active_sessions_of_shutdown()
    assert len(adapter.sent_calls) == 1
    _chat, message, _meta = adapter.sent_calls[0]
    assert message == "⚠️ Hermes is restarting."


@pytest.mark.asyncio
async def test_console_never_this_user_is_a_plain_shutdown(monkeypatch, fleet_root):
    """A Linux service / headless Mac: /dev/console was never the gateway user's, so no page."""
    root, _home = fleet_root
    runner, adapter = make_restart_runner()
    _home_channel(runner)

    _sigterm(monkeypatch, runner, console_at_start=ROOT_UID, console_now=ROOT_UID)

    time.sleep(0.5)
    assert _ledger_rows(root) == []
    await runner._notify_active_sessions_of_shutdown()
    _chat, message, _meta = adapter.sent_calls[0]
    assert message == "⚠️ Hermes is shutting down."


def test_diag_block_lists_processes_with_ps_unavailable(monkeypatch, fleet_root, tmp_path):
    _root, home = fleet_root
    empty_bin = tmp_path / "nobin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))  # no ps, no bash, no timeout
    runner, _adapter = make_restart_runner()
    runner._restart_requested = True
    runner._restart_requester = "restart tool"

    t0 = time.monotonic()
    _sigterm(monkeypatch, runner, console_at_start=OWN_UID, console_now=ROOT_UID)
    assert time.monotonic() - t0 < 1.5

    text = (home / "logs" / "gateway-shutdown-diag.log").read_text()
    block = text[text.index("=== shutdown diagnostic (in-process) @ SIGTERM ==="):]
    assert f"(uid {ROOT_UID})" in block and f"console_user_at_start=" in block
    assert "restart_requested=in-band restart (requester=restart tool)" in block
    listing = block[block.index("--- processes (in-process, no ps;"):].splitlines()
    assert listing[1] == "PID PPID USER STAT COMM"
    assert any(line.split()[0] == str(os.getpid()) for line in listing[2:-1]), listing[:5]
