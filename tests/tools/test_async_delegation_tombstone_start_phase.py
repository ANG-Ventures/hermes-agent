"""Delegation tombstones must say whether the dead attempt ever started.

An owner that dies leaves a terminal record behind, but the record alone cannot
tell an operator which of two very different things happened:

  * the attempt was submitted to an executor and died mid-flight -- real work
    was lost and a redispatch is warranted; or
  * the attempt never reached an executor at all -- nothing ran, nothing was
    lost, and the tombstone is bookkeeping.

Both currently render identically, which trains operators to ignore all of them
-- and that is how a genuine mid-flight loss gets missed.

``attempt.submitted_at`` already carries the discriminating signal: it is set by
``mark_submitted``, the first thing the worker does before invoking the runner,
so ``None`` means the runner provably never ran. ``claim_recoveries`` already
relies on exactly this for redispatch budgeting (the RC-1 branch). These tests
pin that the emitted payloads *surface* the signal instead of making every
consumer re-derive it from the registry.
"""

from __future__ import annotations

import threading
import time

import pytest

from tools import async_delegation as ad

# Reuse the persistence suite's fixtures/builders so the two files cannot drift.
# `_isolated_registry` is autouse; importing it into this module registers it here.
from tests.tools.test_async_delegation_persistence import (  # noqa: F401
    _dispatch,
    _isolated_registry,
    _load,
    _running_record,
    _spec,
    _write_record,
)


def _terminal_events(record):
    return [event for event in record.get("outbox", []) if event["type"] == "async_delegation"]


def _restart_events(record):
    return [
        event
        for event in record.get("outbox", [])
        if event["type"] == "async_delegation_restarted"
    ]


def _await_state(delegation_id, states, timeout=5):
    """Poll the durable record until the worker thread writes a terminal state.

    Establishes happens-before against the executor thread rather than sleeping
    a fixed interval and hoping the local box wins the race.
    """
    deadline = time.time() + timeout
    record = _load()["records"][delegation_id]
    while time.time() < deadline and record.get("state") not in states:
        time.sleep(0.02)
        record = _load()["records"][delegation_id]
    assert record.get("state") in states, (
        f"record never reached {states}; still {record.get('state')!r}"
    )
    return record


def test_terminal_tombstone_names_died_before_start_when_submission_never_landed(monkeypatch):
    """Submission telemetry never landed, so the runner never ran: nothing lost."""
    queued = []

    class QueuingExecutor:
        def submit(self, fn):
            queued.append(fn)
            return object()

    monkeypatch.setattr(ad, "_get_executor", lambda workers: QueuingExecutor())
    monkeypatch.setattr(
        ad._store,
        "mark_submitted",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("lock busy")),
    )
    result = ad.dispatch_async_delegation_batch(
        goals=["continue the report"],
        context=None,
        toolsets=None,
        role="leaf",
        model="test-model",
        session_key="agent:main:telegram:dm:123",
        parent_session_id="parent-1",
        runner=lambda: pytest.fail("runner must not run"),
        durable_spec=_spec(),
        current_boot_id="100:1.0",
    )
    queued[0]()

    record = _load()["records"][result["delegation_id"]]
    assert record["attempt"]["submitted_at"] is None
    events = _terminal_events(record)
    assert events, "expected a terminal tombstone"
    assert events[0]["payload"]["died_before_start"] is True


def test_terminal_tombstone_names_started_when_the_attempt_reached_the_runner():
    """A real completion was submitted, so its record must not claim otherwise."""
    result, gate = _dispatch()
    gate.set()
    record = _await_state(result["delegation_id"], {"done", "failed"})

    assert record["state"] == "done"
    assert record["attempt"]["submitted_at"] is not None
    events = _terminal_events(record)
    assert events, "expected a terminal event"
    assert events[0]["payload"]["died_before_start"] is False


@pytest.mark.parametrize("submitted_at,died_before_start", [(None, True), (1.0, False)])
def test_exhausted_tombstone_names_whether_the_attempt_ever_started(
    monkeypatch, submitted_at, died_before_start
):
    # generation/redispatch_count are chosen so BOTH branches exhaust: the RC-1
    # branch refunds one attempt when submitted_at is None (3 -> 2), and
    # MAX_REDISPATCH_ATTEMPTS is 2, so 2 >= 2 and 3 >= 2 both terminalize.
    record = _running_record(generation=3, redispatch_count=3, submitted_at=submitted_at)
    _write_record(record)
    monkeypatch.setattr(ad, "is_boot_id_alive", lambda boot: False)
    result = ad.recover_async_delegations(
        current_boot_id="400:4.0",
        runner_factory=lambda *args: pytest.fail("exhausted record must not redispatch"),
    )

    assert result["exhausted"] == 1
    current = _load()["records"][record["delegation_id"]]
    assert current["terminal"]["error"] == "restart_attempts_exhausted"
    events = _terminal_events(current)
    assert events, "expected an exhausted tombstone"
    assert events[0]["payload"]["died_before_start"] is died_before_start


@pytest.mark.parametrize("submitted_at,died_before_start", [(None, True), (1.0, False)])
def test_stale_tombstone_names_whether_the_attempt_ever_started(
    monkeypatch, submitted_at, died_before_start
):
    record = _running_record(submitted_at=submitted_at)
    record["created_at"] = time.time() - ad._store.ACTIVE_STALE_SECONDS - 1
    _write_record(record)
    monkeypatch.setattr(ad, "is_boot_id_alive", lambda boot: False)
    ad.recover_async_delegations(
        current_boot_id="500:5.0",
        runner_factory=lambda *args: pytest.fail("stale record must not redispatch"),
    )

    current = _load()["records"][record["delegation_id"]]
    assert current["terminal"]["error"] == "stale_record"
    events = _terminal_events(current)
    assert events, "expected a stale tombstone"
    assert events[0]["payload"]["died_before_start"] is died_before_start


@pytest.mark.parametrize("submitted_at,died_before_start", [(None, True), (1.0, False)])
def test_restart_notice_names_whether_the_superseded_attempt_ever_started(
    monkeypatch, submitted_at, died_before_start
):
    """The restart notice describes the attempt being replaced, not its replacement.

    ``claim_recoveries`` rewrites ``attempt.submitted_at`` to None as part of
    claiming, and only *then* builds the restart payload -- so reading the field
    off the record at payload-build time would report died_before_start=True for
    every restart ever emitted. The payload must carry the superseded attempt's
    value, captured before the claim overwrites it.
    """
    record = _running_record(generation=1, redispatch_count=1, submitted_at=submitted_at)
    _write_record(record)
    monkeypatch.setattr(ad, "is_boot_id_alive", lambda boot: False)
    gate = threading.Event()
    started = threading.Event()

    def resumed_runner():
        started.set()
        return gate.wait(timeout=5) or {"status": "completed"}

    result = ad.recover_async_delegations(
        current_boot_id="200:2.0",
        runner_factory=lambda claimed, continuation: resumed_runner,
        max_async_children=1,
    )
    assert started.wait(timeout=5)
    assert result["claimed"] == 1

    current = _load()["records"][record["delegation_id"]]
    events = _restart_events(current)
    assert events, "expected a restart notice"
    assert events[-1]["payload"]["died_before_start"] is died_before_start
    gate.set()
