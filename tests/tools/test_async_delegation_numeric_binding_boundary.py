"""A persisted timestamp the mirror cannot BIND must be quarantined, not raised.

``_is_optional_number`` accepted any finite JSON number, but the recovery and
replay mirrors bind these values as sqlite3 parameters, and sqlite3 refuses an
int outside signed 64-bit with ``OverflowError: Python int too large to convert
to SQLite INTEGER``. ``terminal.completed_at = 2 ** 100`` is a finite JSON
integer whose ``float()`` succeeds, so it survived validation and then raised
out of the per-record recovery loop -- aborting restoration of every healthy
sibling in the profile.

Both SQL-bound timestamp fields are covered: the record-level
``terminal.completed_at`` (no-envelope mirror path,
``_recover_abandoned_delegations``) and the sibling ``outbox[].payload
.completed_at`` (deliverable path, ``_sync_completion``), which is bound into
the same REAL column and was never validated at all.
"""
from __future__ import annotations

import json
import queue
import sqlite3
from pathlib import Path

import pytest

from tests.tools import test_async_delegation_terminal_receipts as helpers
from tools import async_delegation as ad
from tools import async_delegation_store as store

SQLITE_INT_MAX = 2 ** 63 - 1
SQLITE_INT_MIN = -(2 ** 63)


@pytest.fixture(autouse=True)
def _sut_is_this_worktree():
    root = Path(__file__).resolve().parents[2]
    assert Path(ad.__file__).resolve().is_relative_to(root)
    assert Path(store.__file__).resolve().is_relative_to(root)


def _two_terminal_records(monkeypatch, *, deliverable: bool):
    """Dispatch a bad and a healthy record; drain the in-process queue."""
    bad, bad_worker, _ = helpers.dispatch(monkeypatch)
    good, good_worker, _ = helpers.dispatch(monkeypatch)
    if deliverable:
        bad_worker()
    good_worker()
    while True:
        try:
            helpers.process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
    return bad, good


def _make_no_envelope_terminal(record, completed_at):
    record["state"] = "done"
    record["outbox"] = []
    record["terminal"] = {"status": "completed", "completed_at": completed_at,
                          "result": {"summary": "fixture-only"}}


# --- the confirmed defect, both SQL-bound fields -------------------------


@pytest.mark.parametrize("bad_value", [2 ** 100, SQLITE_INT_MAX + 1,
                                       SQLITE_INT_MIN - 1, -(2 ** 100)])
def test_unbindable_no_envelope_timestamp_quarantines_and_spares_sibling(
    monkeypatch, tmp_path, bad_value
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        bad, good = _two_terminal_records(monkeypatch, deliverable=False)
        with store.locked_registry() as registry:
            _make_no_envelope_terminal(registry["records"][bad], bad_value)

        persisted = json.loads(store.registry_path().read_text())
        assert bad in persisted["records"] and good in persisted["records"]

        q = queue.Queue()
        assert ad.restore_undelivered_completions(q) == 1
        assert [item["delegation_id"] for item in q.queue] == [good]

        # Evidence preserved: the malformed record is neither dropped nor
        # resealed with a normalized timestamp.
        after = json.loads(store.registry_path().read_text())
        assert bad in after["records"]
        assert after["records"][bad]["terminal"]["completed_at"] == bad_value
        assert Path(ad._db_path()).resolve().is_relative_to(tmp_path.resolve())
    finally:
        ad._reset_for_tests()


@pytest.mark.parametrize("bad_value", [2 ** 100, SQLITE_INT_MAX + 1])
def test_unbindable_envelope_payload_timestamp_spares_sibling(
    monkeypatch, tmp_path, bad_value
):
    """The deliverable sibling field is bound by _sync_completion too."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        bad, good = _two_terminal_records(monkeypatch, deliverable=True)
        with store.locked_registry() as registry:
            record = registry["records"][bad]
            envelopes = [e for e in record["outbox"]
                         if e.get("type") == "async_delegation"]
            assert envelopes, "fixture must carry a deliverable envelope"
            for entry in envelopes:
                entry["payload"]["completed_at"] = bad_value

        q = queue.Queue()
        ad.restore_undelivered_completions(q)
        assert good in [item["delegation_id"] for item in q.queue]

        after = json.loads(store.registry_path().read_text())
        assert bad in after["records"]
        payloads = [e["payload"]["completed_at"] for e in after["records"][bad]["outbox"]
                    if e.get("type") == "async_delegation"]
        assert payloads == [bad_value] * len(payloads)
    finally:
        ad._reset_for_tests()


# --- healthy values must still restore (no over-rejection) ---------------


@pytest.mark.parametrize("good_value", [
    1234.5,              # ordinary float timestamp
    1789390471,          # ordinary int timestamp
    0,
    SQLITE_INT_MAX,      # inclusive signed-64 edge
    SQLITE_INT_MIN,      # inclusive signed-64 edge
    float(2 ** 100),     # huge FINITE float: sqlite binds it as REAL
    None,                # null semantics preserved
])
def test_bindable_timestamp_still_restores_both_records(monkeypatch, tmp_path, good_value):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        first, second = _two_terminal_records(monkeypatch, deliverable=False)
        with store.locked_registry() as registry:
            _make_no_envelope_terminal(registry["records"][first], good_value)

        q = queue.Queue()
        ad.restore_undelivered_completions(q)
        assert second in [item["delegation_id"] for item in q.queue]

        # Timestamp identity: a valid value is mirrored, never coerced.
        after = json.loads(store.registry_path().read_text())
        assert after["records"][first]["terminal"]["completed_at"] == good_value
        if good_value is not None:
            with ad._transaction() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT completed_at FROM async_delegations WHERE delegation_id=?",
                    (first,),
                ).fetchone()
            assert row["completed_at"] == pytest.approx(float(good_value))
    finally:
        ad._reset_for_tests()


# --- the validator's own contract ---------------------------------------


@pytest.mark.parametrize("value", [
    None, 0, 1, -1, 1234.5, -1234.5, float(2 ** 100), float(-(2 ** 100)),
    SQLITE_INT_MAX, SQLITE_INT_MIN,
])
def test_validator_accepts_bindable(value):
    assert store._is_optional_number(value) is True


@pytest.mark.parametrize("value", [
    SQLITE_INT_MAX + 1, SQLITE_INT_MIN - 1, 2 ** 100, -(2 ** 100), 10 ** 400,
    True, False,                      # bool is an int subclass: reject
    float("inf"), float("-inf"), float("nan"),
    "1234.5", [1234.5], {"completed_at": 1}, b"1234",
])
def test_validator_rejects_unbindable_and_non_numbers(value):
    assert store._is_optional_number(value) is False


@pytest.mark.parametrize("value", [SQLITE_INT_MAX, SQLITE_INT_MIN, float(2 ** 100)])
def test_accepted_values_really_bind(value):
    """The accept-set is defined by what sqlite3 can bind; prove it."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x REAL)")
    conn.execute("INSERT INTO t VALUES(?)", (value,))


@pytest.mark.parametrize("value", [SQLITE_INT_MAX + 1, SQLITE_INT_MIN - 1, 2 ** 100])
def test_rejected_ints_really_cannot_bind(value):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x REAL)")
    with pytest.raises(OverflowError):
        conn.execute("INSERT INTO t VALUES(?)", (value,))
