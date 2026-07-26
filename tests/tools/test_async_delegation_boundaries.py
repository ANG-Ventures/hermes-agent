"""Boundary coverage for async-delegation parking (salvaged wave3b tests, 2026-07-26).

The park mechanism itself is covered in test_async_delegation.py; these pin the
EDGES: the off-by-one at the attempt cap, mixed batches parking only the
exhausted row, and repeated ownership-drops accumulating to the threshold.
"""
import json
import queue
import time

import pytest

import tools.async_delegation as ad


@pytest.fixture
def delegation_db(tmp_path, monkeypatch):
    """Point the durable delegation store at a temp HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.async_delegation as ad

    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    return ad


def _insert_pending(ad, delegation_id: str, *, attempts: int) -> None:
    """Insert a durable completion in the pending, undelivered state."""
    now = time.time()
    event = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": "telegram:dead-session",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": None,
        "goal": "a task whose origin session is gone",
        "status": "success",
        "summary": "done",
    }
    with ad._DB_LOCK, ad._connect() as conn:
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id, state,
                dispatched_at, completed_at, updated_at, event_json,
                delivery_state, delivery_attempts)
               VALUES (?, ?, '', 'success', ?, ?, ?, ?, 'pending', ?)""",
            (delegation_id, "telegram:dead-session", now, now, now,
             json.dumps(event), attempts),
        )


def _delivery_row(ad, delegation_id: str):
    with ad._DB_LOCK, ad._connect() as conn:
        return conn.execute(
            "SELECT delivery_state, delivery_attempts FROM async_delegations "
            "WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()


class _Queue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def test_boundary_one_attempt_below_budget_is_still_restored(delegation_db):
    """Off-by-one guard: the cap must not retire a row that has budget left."""
    ad = delegation_db
    _insert_pending(ad, "d-nearly", attempts=ad._MAX_DELIVERY_ATTEMPTS - 1)

    queue = _Queue()
    restored = ad.restore_undelivered_completions(queue)

    assert restored == 1
    assert queue.items[0]["delegation_id"] == "d-nearly"


def test_mixed_batch_parks_only_the_exhausted_row(delegation_db):
    """One poisoned row must not suppress recovery of healthy siblings."""
    ad = delegation_db
    _insert_pending(ad, "d-exhausted", attempts=ad._MAX_DELIVERY_ATTEMPTS)
    _insert_pending(ad, "d-healthy", attempts=1)

    queue = _Queue()
    restored = ad.restore_undelivered_completions(queue)

    assert restored == 1
    assert [item["delegation_id"] for item in queue.items] == ["d-healthy"]
    assert _delivery_row(ad, "d-exhausted")[0] == "parked"
    assert _delivery_row(ad, "d-healthy")[0] == "pending"


def test_repeated_ownership_drops_eventually_reach_the_parking_threshold(
    delegation_db,
):
    """End-to-end: the drop path alone must be able to retire a poisoned row."""
    ad = delegation_db
    _insert_pending(ad, "d-unowned", attempts=0)

    for _ in range(ad._MAX_DELIVERY_ATTEMPTS):
        ad._note_delivery_attempt("d-unowned")

    queue = _Queue()
    ad.restore_undelivered_completions(queue)

    assert queue.items == []
    assert _delivery_row(ad, "d-unowned")[0] == "parked"
