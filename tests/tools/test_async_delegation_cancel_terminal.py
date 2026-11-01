"""A durable cancel is terminal evidence: recovery mirrors it instead of warning every sweep.

Regression for deleg_df721d2b (aegis, 2026-10-26): ``cancel_matching`` sets ``state=cancelled``
with no ``terminal`` block, so every ~30 s recovery sweep logged "lacks execution evidence"
(15k lines) and the SQLite mirror stayed ``running`` forever.
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
from types import SimpleNamespace

import pytest

from tools import async_delegation as ad
from tools import async_delegation_store as store
from tools.process_registry import process_registry

_WARNING = "lacks execution evidence"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    yield
    ad._reset_for_tests()


def _dispatch(monkeypatch, session_key):
    monkeypatch.setattr(ad, "_get_executor", lambda *a: SimpleNamespace(submit=lambda worker: None))
    spec = {
        "profile": "default",
        "source": {"kind": "single", "tasks": [{"goal": "cancel me"}]},
        "execution": {"model": "test-model", "provider": "test-provider"},
        "route": {"session_key": session_key, "parent_session_id": "p", "platform": "telegram"},
    }
    dispatched = ad.dispatch_async_delegation(
        goal="cancel me", context=None, toolsets=None, role="leaf", model="test-model",
        session_key=session_key, runner=lambda: {"status": "completed"},
        durable_spec=spec, current_boot_id="100:1.0")
    assert dispatched["status"] == "dispatched", dispatched
    assert store.cancel_matching(session_key=session_key, reason="stop_command", caller="test") == 1
    return dispatched["delegation_id"]


def _record(delegation_id):
    return json.loads(ad._registry_path().read_text())["records"][delegation_id]


def _mirror(delegation_id):
    with ad._transaction() as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?",
                                 (delegation_id,)).fetchone())


def _warnings(caplog, delegation_id):
    return [r for r in caplog.records
            if r.levelno >= logging.WARNING and _WARNING in r.getMessage() and delegation_id in r.getMessage()]


def test_attributed_cancel_resolves_mirror_on_first_pass(monkeypatch, caplog):
    identifier = _dispatch(monkeypatch, "cancel-attributed")
    record = _record(identifier)
    # terminal stays None: self_heal_delegations reads a dict terminal as "delivered" and would
    # stop salvaging the cancelled child's work.
    assert record["terminal"] is None
    cancelled_at = record["cancel_attribution"]["cancelled_at"]

    ad.recover_abandoned_delegations()
    mirror = _mirror(identifier)
    assert mirror["state"] == "cancelled"
    assert mirror["completed_at"] == cancelled_at
    assert json.loads(mirror["result_json"])["error"] == "stop_command"
    ad.recover_abandoned_delegations()
    assert not _warnings(caplog, identifier)


def test_cancel_without_attribution_resolves_from_lifecycle_event(monkeypatch, caplog):
    identifier = _dispatch(monkeypatch, "cancel-lifecycle")
    with store.locked_registry() as registry:  # the write re-checksums the record
        registry["records"][identifier].pop("cancel_attribution")
    cancelled_ts = [e["ts"] for e in _record(identifier)["lifecycle"] if e["event"] == "cancelled"][-1]

    ad.recover_abandoned_delegations()
    mirror = _mirror(identifier)
    assert mirror["state"] == "cancelled"
    assert mirror["completed_at"] == cancelled_ts
    assert not _warnings(caplog, identifier)


def test_terminal_record_without_any_evidence_warns_once_per_boot(monkeypatch, caplog):
    identifier = _dispatch(monkeypatch, "cancel-no-evidence")
    with store.locked_registry() as registry:
        record = registry["records"][identifier]
        record["terminal"] = None
        record.pop("cancel_attribution")
        record["lifecycle"] = [e for e in record["lifecycle"] if e.get("event") != "cancelled"]

    for _ in range(3):
        ad.recover_abandoned_delegations()
    assert len(_warnings(caplog, identifier)) == 1
    assert _mirror(identifier)["state"] == "running"
