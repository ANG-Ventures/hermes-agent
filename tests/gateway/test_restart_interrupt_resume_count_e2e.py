"""stop() drain timeout -> next boot: resume count == interrupted count.

Card t_e253d9d5 ASK 1 part 2 / Argus r1 finding 2. Incident 2026-09-23
(Apollo 49726 -> 7734): turns still running at the restart drain cap were
interrupted, ``.clean_shutdown`` was skipped, and on the next boot only 1
session was auto-resumed while the rest sat idle until Ace re-messaged each.

Real seams, no mocked store: stop() runs its real drain + interrupt + mark
pass against an on-disk SessionStore; a SECOND runner then loads that store
from disk and runs the real boot sequence (_recover_unclean_sessions when the
marker is absent, then _schedule_resume_pending_sessions). The oracle is what
the boot actually scheduled (adapter.handle_message receipts), not a flag the
code under test sets for itself.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.session import SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

INTERRUPTED = 3
ACTIVE_AT_REQUEST = 16


def _store(path) -> SessionStore:
    return SessionStore(sessions_dir=path, config=GatewayConfig())


def _write_interrupt_intent(home, drain_cap_s: float) -> None:
    row = {
        "event": "intent",
        "busy_policy": "interrupt",
        "drain_cap_s": drain_cap_s,
        "target_profile": "default",
        "initiator_profile": "apollo",
        "origin_mode": "external",
        "token": "3f9a7d6c34f7",
        "pid_before": os.getpid(),
        "epoch": round(time.time(), 3),
    }
    with (home / "logs" / "gateway-restart-ledger.jsonl").open("a") as fh:
        fh.write(json.dumps(row) + "\n")


@pytest.mark.asyncio
async def test_every_turn_interrupted_by_restart_drain_is_auto_resumed_on_next_boot(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    (tmp_path / "logs").mkdir()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # ---- life 1: the incident shape ----------------------------------------
    store = _store(sessions_dir)
    keys = []
    for i in range(ACTIVE_AT_REQUEST):
        entry = store.get_or_create_session(make_restart_source(chat_id=f"90{i:02d}"))
        keys.append(entry.session_key)
    # Long-running turns: last transcript write well outside the boot's 120s
    # recency fallback, so only stop()'s own mark can carry them to the boot.
    stale = datetime.now() - timedelta(minutes=10)
    with store._lock:
        for k in keys:
            store._entries[k].updated_at = stale
        store._save()

    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner.session_store = store
    runner._restart_after_turn_timeout = 1800.0
    runner._restart_drain_timeout = 0.05
    for k in keys:
        runner._running_agents[k] = MagicMock()

    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ):
        assert runner.request_restart(detached=False, via_service=True) is True
        await asyncio.sleep(0.2)
        for k in keys[INTERRUPTED:]:  # 13 finish during the after-turn wait
            del runner._running_agents[k]
        _write_interrupt_intent(tmp_path, drain_cap_s=0.3)
        await asyncio.wait_for(runner._restart_task, timeout=15.0)

    interrupted = keys[:INTERRUPTED]
    assert not (tmp_path / ".clean_shutdown").exists(), (
        "precondition: interrupted chat turns must suppress the clean marker"
    )

    # ---- life 2: fresh runner, store reloaded from disk, real boot path ----
    boot, boot_adapter = make_restart_runner()
    boot.session_store = _store(sessions_dir)
    received = []

    async def _record(event):
        received.append(boot._session_key_for_source(event.source))

    boot_adapter.handle_message = _record
    boot._persist_active_agents = lambda: None

    await GatewayRunner._recover_unclean_sessions(boot)
    GatewayRunner._schedule_resume_pending_sessions(boot)
    for _ in range(100):
        if len(set(received) & set(interrupted)) >= INTERRUPTED:
            break
        await asyncio.sleep(0.02)

    with boot.session_store._lock:
        entries = {k: boot.session_store._entries[k] for k in interrupted}
    suspended = [k for k, e in entries.items() if e.suspended]
    resumed = sorted(set(received) & set(interrupted))

    assert suspended == [], f"interrupted sessions were suspended: {suspended}"
    assert len(resumed) == INTERRUPTED, (
        f"resume count {len(resumed)} != interrupted count {INTERRUPTED}; "
        f"resumed={resumed} reasons={ {k: (e.resume_pending, e.resume_reason) for k, e in entries.items()} }"
    )
