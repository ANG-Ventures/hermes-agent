"""Terminal execution state must not resurrect an accepted delivery receipt."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    yield
    ad._reset_for_tests()


def row(delegation_id):
    with ad._transaction() as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute(
            "SELECT * FROM async_delegations WHERE delegation_id=?", (delegation_id,)
        ).fetchone())


def dispatch(monkeypatch, *, batch=False, result=None, native=False):
    workers = []
    executor = ad._get_executor(1) if native else None

    def submit(worker):
        if executor is None:
            workers.append(worker)
            return None
        future = executor.submit(worker)
        workers.append(lambda: future.result(timeout=10))
        return future

    monkeypatch.setattr(ad, "_get_executor", lambda *args: SimpleNamespace(submit=submit))
    result = result or {"status": "completed", "summary": "receipt-nonce"}
    spec = {
        "profile": "default",
        "source": {"kind": "batch" if batch else "single", "tasks": [{"goal": "receipt"}]},
        "execution": {"model": "test-model", "provider": "test-provider"},
        "route": {"session_key": "receipt-session", "parent_session_id": "receipt-parent",
                  "platform": "telegram"},
    }
    kwargs: dict[str, Any] = dict(context=None, toolsets=None, role="leaf", model="test-model",
                  session_key="receipt-session", runner=lambda: result,
                  durable_spec=spec, current_boot_id="100:1.0")
    if batch:
        dispatched = ad.dispatch_async_delegation_batch(goals=["receipt"], **kwargs)
    else:
        dispatched = ad.dispatch_async_delegation(goal="receipt", **kwargs)
    assert dispatched["status"] == "dispatched"
    return dispatched["delegation_id"], workers.pop(), result


def registry_record(delegation_id):
    return json.loads(ad._registry_path().read_text())["records"][delegation_id]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_restartable_completion_synchronizes_before_receipt(monkeypatch, batch, native):
    observed = []
    put = process_registry.completion_queue.put

    def inspect_publication(event):
        observed.append(row(event["delegation_id"])["state"])
        put(event)

    monkeypatch.setattr(process_registry.completion_queue, "put", inspect_publication)
    delegation_id, worker, result = dispatch(monkeypatch, batch=batch, native=native)
    worker()
    event = process_registry.completion_queue.get_nowait()
    assert ad._records[delegation_id]["status"] == "completed"
    assert row(delegation_id)["state"] == "completed"
    assert registry_record(delegation_id)["state"] == "done"
    claim = ad.claim_event_delivery(event, "test")
    assert claim
    ad.complete_event_delivery(event, claim)
    assert observed == ["completed"]
    assert ad._records[delegation_id]["status"] == "completed"
    assert row(delegation_id)["state"] == "completed"
    assert row(delegation_id)["delivery_state"] == "delivered"
    assert json.loads(row(delegation_id)["result_json"]) == result
    assert registry_record(delegation_id)["state"] == "done"
    assert registry_record(delegation_id)["outbox"][0]["state"] == "delivered"
    assert process_registry.completion_queue.empty()
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET owner_pid=NULL")
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


def legacy(delegation_id, disposition="pending", *, pid=None):
    ad._persist_dispatch({"delegation_id": delegation_id, "dispatched_at": time.time()})
    with ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=?, delivery_state=? WHERE delegation_id=?",
            (pid, disposition, delegation_id),
        )


@pytest.mark.parametrize("disposition", ["delivered", "dropped", "parked"])
def test_mixed_owner_loss_preserves_receipts(monkeypatch, disposition):
    legacy("accepted", disposition)
    legacy("lost")
    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    event = restored.get_nowait()
    assert event["delegation_id"] == "lost"
    assert event["status"] == "unknown"
    assert row("accepted")["delivery_state"] == disposition
    claim = ad.claim_event_delivery(event, "test")
    assert claim
    ad.complete_event_delivery(event, claim)
    assert ad.restore_undelivered_completions(restored) == 0


def test_owner_alive_not_recovered(monkeypatch):
    legacy("alive", pid=123)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET owner_started_at=NULL")
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    assert row("alive")["state"] == "running"


def test_ack_winning_after_recovery_select_is_preserved(monkeypatch):
    legacy("late", pid=123)
    assert ad.claim_completion_delivery("late", "consumer")

    def dead_after_ack(pid):
        # Run the real claimed receipt writer in another process;
        # the reaper's Python lock cannot protect against this writer.
        completed = subprocess.run(
            [sys.executable, "-c", "from tools import async_delegation as ad; "
             "assert ad.complete_completion_delivery('late', 'consumer')"],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "HERMES_HOME": str(ad._db_path().parent)},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        return False

    monkeypatch.setattr("gateway.status._pid_exists", dead_after_ack)
    assert ad.recover_abandoned_delegations() == 0
    assert row("late")["delivery_state"] == "delivered"
    assert row("late")["event_json"] is None


@pytest.mark.parametrize("disposition", ["delivered", "dropped", "parked"])
def test_terminal_sync_does_not_reset_receipt(disposition):
    legacy("accepted", disposition)
    ad._persist_completion(
        {"delegation_id": "accepted", "status": "completed"},
        {"summary": "actual-result"},
    )
    assert row("accepted")["delivery_state"] == disposition
    assert row("accepted")["state"] == "completed"


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("status", ["error", "interrupted"])
def test_unsuccessful_result_finishes_both_stores(monkeypatch, batch, status):
    result = {"status": status, "summary": "partial", "error": "stopped"}
    if batch:
        result = {"results": [result]}
    delegation_id, worker, _ = dispatch(monkeypatch, batch=batch, result=result)
    worker()
    expected = "error" if batch else status
    assert row(delegation_id)["state"] == expected
    assert json.loads(row(delegation_id)["result_json"]) == result
    assert ad._records[delegation_id]["status"] == expected
    assert registry_record(delegation_id)["state"] == "failed"
    event = process_registry.completion_queue.get_nowait()
    assert event["status"] == expected
    assert process_registry.completion_queue.empty()


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("failures", [1, 3])
def test_mirror_failure_bounded_without_losing_canonical_result(monkeypatch, caplog, batch, failures):
    delegation_id, worker, result = dispatch(monkeypatch, batch=batch)
    persist = ad._persist_completion
    calls = []

    def fail_mirror(event, value):
        calls.append(event["event_id"])
        if len(calls) <= failures:
            raise sqlite3.OperationalError("injected mirror failure")
        return persist(event, value)

    monkeypatch.setattr(ad, "_persist_completion", fail_mirror)
    worker()
    assert len(calls) == min(failures + 1, 3)
    assert "terminal mirror failed" in caplog.text
    assert registry_record(delegation_id)["state"] == "done"
    assert ad._records[delegation_id]["status"] == "completed"
    event = process_registry.completion_queue.get_nowait()
    assert event["summary"] == result["summary"]
    assert process_registry.completion_queue.empty()
    if failures == 1:
        assert row(delegation_id)["state"] == "completed"
    claim = ad.claim_event_delivery(event, "test")
    assert claim
    ad.complete_event_delivery(event, claim)
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET owner_pid=NULL")
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


@pytest.mark.parametrize("batch", [False, True])
def test_queue_failure_retains_results_and_finishes_memory(monkeypatch, caplog, batch):
    delegation_id, worker, result = dispatch(monkeypatch, batch=batch)

    def fail_put(event):
        raise RuntimeError("injected queue failure")

    monkeypatch.setattr(process_registry.completion_queue, "put", fail_put)
    worker()
    assert "terminal result retained in outbox" in caplog.text
    assert ad._records[delegation_id]["status"] == "completed"
    assert json.loads(row(delegation_id)["result_json"]) == result
    assert registry_record(delegation_id)["outbox"][0]["state"] == "pending"


def test_explicit_finalization_home_does_not_touch_root(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    secondary = tmp_path / "profiles" / "secondary"
    token = set_hermes_home_override(secondary)
    try:
        delegation_id, _, result = dispatch(monkeypatch)
        attempt = ad._records[delegation_id]["attempt_id"]
        registry_path = ad._registry_path()
    finally:
        reset_hermes_home_override(token)
    legacy(delegation_id)
    # Recovery can finalize under a root caller with an explicit secondary path.
    ad._finalize(delegation_id, result, "completed", attempt_id=attempt, registry_path=registry_path)
    assert get_hermes_home() == tmp_path
    assert row(delegation_id)["state"] == "running"
    token = set_hermes_home_override(secondary)
    try:
        assert row(delegation_id)["state"] == "completed"
        event = process_registry.completion_queue.get_nowait()
        claim = ad.claim_event_delivery(event, "test")
        assert claim
        ad.complete_event_delivery(event, claim)
        with ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET owner_pid=NULL")
        assert ad.restore_undelivered_completions(queue.Queue()) == 0
        assert registry_record(delegation_id)["outbox"][0]["state"] == "delivered"
    finally:
        reset_hermes_home_override(token)
    assert row(delegation_id)["delivery_state"] == "pending"


def test_import_provenance():
    root = Path(__file__).resolve().parents[2]
    assert Path(ad.__file__).resolve() == root / "tools" / "async_delegation.py"
    assert Path(ad._store.__file__).resolve() == root / "tools" / "async_delegation_store.py"


@pytest.mark.parametrize("disposition", ["delivered", "dropped", "parked", "claimed"])
@pytest.mark.parametrize("secondary", [False, True])
def test_recovered_result_does_not_inherit_owner_loss_receipt(monkeypatch, tmp_path, disposition, secondary):
    root_home = ad._db_path().parent
    if secondary:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "secondary"))
    delegation_id, _, result = dispatch(monkeypatch)
    if secondary:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        token = set_hermes_home_override(root_home)
        try:
            legacy(delegation_id, "parked")
        finally:
            reset_hermes_home_override(token)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    old = restored.get_nowait()
    old_claim = ad.claim_event_delivery(old, "owner-loss")
    assert old_claim
    if disposition == "delivered":
        ad.complete_event_delivery(old, old_claim)
    elif disposition == "dropped":
        assert ad.drop_completion_delivery(delegation_id, old_claim)
    elif disposition == "parked":
        with ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET delivery_state='parked'")
    workers = []
    monkeypatch.setattr(ad, "_get_executor", lambda *args: SimpleNamespace(
        submit=lambda fn: workers.append(fn)))
    monkeypatch.setattr(ad, "is_boot_id_alive", lambda boot: False)
    stats = ad.recover_async_delegations(
        current_boot_id="200:2.0", profile_home=ad._db_path().parent,
        runner_factory=lambda record, note: lambda: result,
    )
    assert stats["failed_validation"] == 0
    assert len(workers) == 1
    workers[0]()
    new = process_registry.completion_queue.get_nowait()
    assert new["attempt_generation"] == 1
    assert new["summary"] == result["summary"]
    assert row(delegation_id)["delivery_state"] == "pending"
    assert row(delegation_id)["delivery_attempts"] == 0
    assert not ad.complete_completion_delivery(delegation_id, old_claim)
    assert ad.claim_event_delivery(old, "late-owner-loss") is None
    claim = ad.claim_event_delivery(new, "recovered-result")
    assert claim
    ad.complete_event_delivery(new, claim)
    event = next(e for e in registry_record(delegation_id)["outbox"]
                 if e["event_id"] == new["event_id"])
    assert event["state"] == "delivered"
    assert row(delegation_id)["delivery_state"] == "delivered"
    if secondary:
        monkeypatch.setenv("HERMES_HOME", str(root_home))
        assert row(delegation_id)["delivery_state"] == "parked"
        assert row(delegation_id)["event_json"] is None


@pytest.mark.parametrize("generation", [0, 1])
def test_acceptance_before_mirror_sync_binds_current_identity(monkeypatch, generation):
    delegation_id, worker, _ = dispatch(monkeypatch)
    persist = ad._persist_completion
    accepted = []

    def accept_before_sync(event, result):
        # The canonical outbox is already durable and can be replayed here.
        claim = ad.claim_event_delivery(event, "early-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        accepted.append(dict(event))
        persist(event, result)

    if generation:
        workers = []
        monkeypatch.setattr(ad, "_get_executor", lambda *args: SimpleNamespace(
            submit=lambda fn: workers.append(fn)))
        monkeypatch.setattr(ad, "is_boot_id_alive", lambda boot: False)
        stats = ad.recover_async_delegations(
            current_boot_id="200:2.0", profile_home=ad._db_path().parent,
            runner_factory=lambda record, note: lambda: {"summary": "recovered"},
        )
        assert stats["failed_validation"] == 0
        worker = workers.pop()
    monkeypatch.setattr(ad, "_persist_completion", accept_before_sync)
    worker()
    assert len(accepted) == 1
    assert accepted[0]["attempt_generation"] == generation
    assert row(delegation_id)["delivery_state"] == "delivered"
    assert json.loads(row(delegation_id)["event_json"])["event_id"] == accepted[0]["event_id"]


@pytest.mark.parametrize("generation", [0, 1])
@pytest.mark.parametrize("disposition", ["delivered", "dropped", "parked", "claimed"])
def test_event_replacement_invalidates_only_old_receipt(generation, disposition):
    legacy("replace")
    old = {"type": "async_delegation", "delegation_id": "replace",
           "event_id": "old", "attempt_generation": generation}
    new = {**old, "event_id": "new"}
    ad._persist_completion(old, {})
    old_claim = ad.claim_event_delivery(old, "old")
    assert old_claim
    if disposition == "delivered":
        assert ad.complete_completion_delivery("replace", old_claim)
    elif disposition == "dropped":
        assert ad.drop_completion_delivery("replace", old_claim)
    elif disposition == "parked":
        with ad._transaction() as conn:
            conn.execute("UPDATE async_delegations SET delivery_state='parked'")
    ad._persist_completion(old, {})
    assert row("replace")["delivery_state"] == (
        "pending" if disposition == "claimed" else disposition
    )
    if disposition == "claimed":
        assert row("replace")["delivery_claim"] == old_claim
    ad._persist_completion(new, {})
    assert row("replace")["delivery_state"] == "pending"
    assert row("replace")["delivery_claim"] is None
    assert row("replace")["delivered_at"] is None
    assert not ad.complete_completion_delivery("replace", old_claim)
    assert not ad.release_completion_delivery("replace", old_claim)
    assert not ad.drop_completion_delivery("replace", old_claim)
    assert ad.claim_event_delivery(old, "late-old") is None
    current = ad.claim_event_delivery(new, "new")
    assert current
    ad._persist_completion(new, {})
    assert row("replace")["delivery_claim"] == current
    assert ad.complete_completion_delivery("replace", current)
    ad._persist_completion(new, {})
    assert row("replace")["delivery_state"] == "delivered"


def test_legacy_identity_survives_restore_but_not_new_result():
    legacy("legacy")
    old = {"type": "async_delegation", "delegation_id": "legacy",
           "status": "unknown", "completed_at": time.time()}
    ad._persist_completion(old, {})
    claim = ad.claim_event_delivery({**old, "restored": True}, "restored")
    assert claim
    assert ad.complete_completion_delivery("legacy", claim)
    ad._persist_completion(old, {})
    assert row("legacy")["delivery_state"] == "delivered"
    new = {**old, "status": "completed", "completed_at": old["completed_at"] + 1}
    ad._persist_completion(new, {})
    assert row("legacy")["delivery_state"] == "pending"
    assert ad.claim_event_delivery(old, "stale") is None
    assert ad.claim_event_delivery(new, "actual")


def test_missing_identity_cannot_transfer_legacy_receipt_to_outbox():
    legacy("legacy", "delivered")
    event = {"type": "async_delegation", "delegation_id": "legacy",
             "event_id": "first-known-event", "attempt_generation": 0}
    ad._persist_completion(event, {})
    assert row("legacy")["delivery_state"] == "pending"
    assert ad.claim_event_delivery(event, "new")


def test_same_event_id_different_attempt_has_no_receipt():
    legacy("attempt")
    old = {"type": "async_delegation", "delegation_id": "attempt",
           "event_id": "event", "attempt_generation": 0}
    new = {**old, "attempt_generation": 1}
    ad._persist_completion(old, {})
    claim = ad.claim_event_delivery(old, "old")
    assert claim
    assert ad.complete_completion_delivery("attempt", claim)
    ad._persist_completion(new, {})
    assert row("attempt")["delivery_state"] == "pending"
    assert ad.claim_event_delivery(old, "old") is None
    assert ad.claim_event_delivery(new, "new")


def test_late_old_claim_in_other_process_cannot_accept_replacement():
    legacy("cross-process")
    old = {"type": "async_delegation", "delegation_id": "cross-process",
           "event_id": "old", "attempt_generation": 0}
    new = {**old, "event_id": "new", "attempt_generation": 1}
    ad._persist_completion(old, {})
    claim = ad.claim_event_delivery(old, "old")
    assert claim
    ad._persist_completion(new, {})
    completed = subprocess.run(
        [sys.executable, "-c",
         "import json,sys; from tools import async_delegation as ad; "
         "evt=json.loads(sys.argv[1]); "
         "assert not ad.complete_completion_delivery(evt['delegation_id'],sys.argv[2]); "
         "assert ad.claim_event_delivery(evt,'late') is None",
         json.dumps(old), claim],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HERMES_HOME": str(ad._db_path().parent)},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert row("cross-process")["delivery_state"] == "pending"
    assert ad.claim_event_delivery(new, "current")


def test_owner_loss_replacement_invalidates_early_claim():
    legacy("early")
    early = {"type": "async_delegation", "delegation_id": "early",
             "event_id": "terminal", "attempt_generation": 0}
    claim = ad.claim_event_delivery(early, "before-mirror")
    assert claim
    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    assert not ad.complete_completion_delivery("early", claim)
    assert ad.claim_event_delivery(early, "late") is None
    unknown = restored.get_nowait()
    assert unknown["status"] == "unknown"
    assert ad.claim_event_delivery(unknown, "recovery")
