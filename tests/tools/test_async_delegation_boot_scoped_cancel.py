"""A one-shot exit must not durably cancel another LIVE boot's delegations.

``hermes_cli/main.py:_cleanup_oneshot_runtime`` calls ``interrupt_all`` on every
one-shot CLI exit, which took the terminal branch and called
``cancel_matching(all_active=True)`` — a write against the process-SHARED
registry with no owner check. The in-memory half of ``interrupt_all`` was
already boot-scoped (it only signals records in its own ``_records``); the
durable half was not, so a ``hermes -z`` cancelled every in-flight delegation
the live gateway owned. These tests pin the durable half to the same scope.

A record owned by a DEAD boot stays cancellable — that is the crash-recovery
path and must not regress.
"""

from __future__ import annotations

import json
import time

import pytest

from gateway.status import get_current_boot_id
from tools import async_delegation as ad
from tools.process_registry import process_registry

DEAD_BOOT = "2147483646:1.0"  # pid that cannot be running with that create_time


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield tmp_path
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _record(delegation_id: str, owner_boot_id: str, state: str = "running") -> dict:
    now = time.time()
    return {
        "delegation_id": delegation_id,
        "state": state,
        "created_at": now,
        "updated_at": now,
        "profile": "default",
        "source": {
            "kind": "single",
            "tasks": [{
                "goal": "finish the port",
                "context": "",
                "role": "leaf",
                "inherit_context": False,
            }],
            "shared_context": None,
        },
        "execution": {
            "model": "test-model",
            "provider": "test-provider",
            "base_url": "https://example.invalid/v1",
            "api_mode": "chat_completions",
            "toolsets": ["file"],
            "max_iterations": 50,
            "parent_depth": 0,
            "workspace_hint": "/tmp",
            "credential_ref": {"provider": "test-provider", "custom_provider": None},
        },
        "route": {
            "session_key": "agent:main:telegram:dm:123",
            "parent_session_id": "parent-1",
            "origin_ui_session_id": "",
            "platform": "telegram",
            "chat_type": "dm",
            "chat_id": "123",
            "session_id": None,
            "user_id": "u1",
            "user_name": "Ace",
            "profile": "default",
        },
        "attempt": {
            "attempt_id": f"{delegation_id}:g0:a",
            "generation": 0,
            "redispatch_count": 0,
            "owner_boot_id": owner_boot_id,
            "started_at": now,
            "submitted_at": now,
            "last_interrupted_at": None,
            "last_error": None,
        },
        "terminal": None,
        "outbox": [],
    }


def _write(record: dict) -> None:
    ad._write_registry_for_tests({
        "schema_version": 1,
        "updated_at": time.time(),
        "records": {record["delegation_id"]: record},
    })


def _load(delegation_id: str) -> dict:
    registry = json.loads(ad._registry_path().read_text(encoding="utf-8"))
    return registry["records"][delegation_id]


def test_oneshot_exit_leaves_live_other_boots_record_running():
    """1. Boot B's one-shot shutdown must not touch boot A's live record."""
    _write(_record("deleg_live", owner_boot_id=get_current_boot_id()))

    ad.interrupt_all(reason="oneshot shutdown", boot_id=DEAD_BOOT)

    stored = _load("deleg_live")
    assert stored["state"] == "running"
    assert "cancel_attribution" not in stored


def test_dead_owner_record_is_still_cancelled():
    """2. Crash recovery preserved: a dead boot's record stays cancellable."""
    _write(_record("deleg_dead", owner_boot_id=DEAD_BOOT))

    ad.interrupt_all(reason="oneshot shutdown", boot_id=get_current_boot_id())

    stored = _load("deleg_dead")
    assert stored["state"] == "cancelled"
    assert stored["cancel_attribution"]["reason"] == "oneshot shutdown"


def test_oneshot_cancels_the_records_it_owns_itself():
    """3. A one-shot that spawned its own delegation still cancels it."""
    mine = get_current_boot_id()
    _write(_record("deleg_mine", owner_boot_id=mine))

    ad.interrupt_all(reason="oneshot shutdown", boot_id=mine)

    stored = _load("deleg_mine")
    assert stored["state"] == "cancelled"
    assert stored["cancel_attribution"]["selector"] == {"all_active": True}


def test_legacy_record_without_owner_boot_id_is_still_cancelled():
    """Records predating owner_boot_id keep the old terminal behaviour."""
    record = _record("deleg_legacy", owner_boot_id="")
    record["attempt"].pop("owner_boot_id")
    _write(record)

    ad.interrupt_all(reason="oneshot shutdown", boot_id=DEAD_BOOT)

    assert _load("deleg_legacy")["state"] == "cancelled"


def test_stop_semantics_unchanged_for_session_scoped_cancel():
    """4. /stop of the caller's own session stays terminal, live owner or not."""
    _write(_record("deleg_stop", owner_boot_id=get_current_boot_id()))

    ad.interrupt_for_session(parent_session_id="parent-1", reason="stop_command")

    stored = _load("deleg_stop")
    assert stored["state"] == "cancelled"
    assert stored["cancel_attribution"]["reason"] == "stop_command"


def test_spared_record_can_still_deliver_its_terminal_result():
    """5. The whole point: the child's completed result reaches the outbox."""
    _write(_record("deleg_finish", owner_boot_id=get_current_boot_id()))

    ad.interrupt_all(reason="oneshot shutdown", boot_id=DEAD_BOOT)

    updated = ad._store.append_terminal(
        "deleg_finish",
        "deleg_finish:g0:a",
        {"status": "completed", "summary": "branch pushed"},
        "completed",
    )

    assert updated is not None
    stored = _load("deleg_finish")
    assert stored["state"] == "done"
    assert isinstance(stored["terminal"], dict)
    assert any(
        event.get("type") == "async_delegation" and event.get("state") == "pending"
        for event in stored["outbox"]
    )
