"""Owner-exit tombstones must distinguish real loss from bookkeeping.

The reaper (``recover_abandoned_delegations``) sees two OPPOSITE situations:

* the owner died with children still unaccounted for — real lost work that
  needs re-dispatch; and
* the owner died after its consolidated result was already delivered to the
  parent surface — pure bookkeeping, nothing was lost.

Historically both produced a byte-identical terminal record
("Delegation owner exited before recording a terminal result; outcome
unknown."), so a run of harmless bookkeeping records trained the reader to
ignore the one that mattered.

These are behavior contracts, not snapshots: they assert how the tombstone
must RELATE to the per-child state the store already holds, never a frozen
message string.
"""

from __future__ import annotations

import json
import sqlite3

import pytest


@pytest.fixture()
def ad(tmp_path, monkeypatch):
    """Import the module against an isolated HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    import tools.async_delegation as module

    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    # Default the config gate on; individual tests override it. raising=False
    # so a build WITHOUT the fix fails on BEHAVIOR (the assertions below),
    # not on a missing symbol — the RED must prove the contract, not the shape.
    monkeypatch.setattr(
        module, "_suppress_delivered_tombstones", lambda: True, raising=False
    )
    return module


def _seed(
    ad,
    delegation_id: str,
    *,
    delivery_state: str,
    result: dict | None,
    goals: list[str],
    state: str = "running",
) -> None:
    """Insert a row owned by a pid that cannot be alive."""
    with ad._connect() as conn:
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id, result_json)
               VALUES (?, 'sk', '', NULL, ?, 1.0, 1.0, ?, 0, ?, NULL, ?, '', ?)""",
            (
                delegation_id,
                state,
                delivery_state,
                2_000_000_000,  # a pid that cannot exist
                json.dumps({"goals": goals, "is_batch": True}),
                json.dumps(result) if result is not None else None,
            ),
        )
        conn.commit()


def _row(ad, delegation_id: str) -> sqlite3.Row:
    with ad._connect() as conn:
        return conn.execute(
            """SELECT state, delivery_state, event_json, result_json
               FROM async_delegations WHERE delegation_id=?""",
            (delegation_id,),
        ).fetchone()


def _event(ad, delegation_id: str) -> dict:
    return json.loads(_row(ad, delegation_id)[2] or "{}")


# ---------------------------------------------------------------------------
# Direction 1 — owner killed MID-FLIGHT: must be LOUD and name the children
# ---------------------------------------------------------------------------

def test_mid_flight_owner_exit_names_unaccounted_children(ad):
    """A batch interrupted mid-work must name WHICH children died."""
    _seed(
        ad,
        "deleg_lost",
        delivery_state="pending",
        result={
            "results": [
                {"status": "interrupted", "exit_reason": "interrupted", "goal": "alpha"},
                {"status": "completed", "exit_reason": "completed", "goal": "bravo"},
                {"status": "interrupted", "exit_reason": "interrupted", "goal": "charlie"},
            ]
        },
        goals=["alpha", "bravo", "charlie"],
    )

    assert ad.recover_abandoned_delegations() == 1

    state, delivery_state, _, result_json = _row(ad, "deleg_lost")
    event = _event(ad, "deleg_lost")

    # It re-enters the conversation.
    assert delivery_state == "pending"
    assert state == "unknown"
    assert event["owner_exit_delivered"] is False

    # It names the unaccounted children — by index and by goal — and does NOT
    # implicate the child that finished.
    unaccounted = event["unaccounted_children"]
    assert [c["index"] for c in unaccounted] == [0, 2]
    assert {c["goal"] for c in unaccounted} == {"alpha", "charlie"}
    assert all(c["status"] == "interrupted" for c in unaccounted)

    # The message must carry the actionable content, not a fixed literal.
    for token in ("#0", "#2", "re-dispatch", "interrupted"):
        assert token in event["error"], (token, event["error"])
    assert "#1" not in event["error"]

    # Per-child terminal states are preserved in BOTH the event and the result.
    assert [c["status"] for c in event["child_states"]] == [
        "interrupted", "completed", "interrupted",
    ]
    assert json.loads(result_json)["child_states"] == event["child_states"]


def test_child_that_never_reported_is_counted_unaccounted(ad):
    """A dispatched goal with no recorded result is the clearest loss case."""
    _seed(
        ad,
        "deleg_partial",
        delivery_state="pending",
        result={"results": [{"status": "completed", "goal": "alpha"}]},
        goals=["alpha", "bravo"],
    )

    assert ad.recover_abandoned_delegations() == 1
    event = _event(ad, "deleg_partial")

    assert len(event["child_states"]) == 2
    assert [c["index"] for c in event["unaccounted_children"]] == [1]
    assert event["child_states"][1]["goal"] == "bravo"


def test_no_recorded_child_state_still_reports_unknown(ad):
    """With nothing recorded, the honest answer is still 'outcome unknown'."""
    _seed(ad, "deleg_bare", delivery_state="pending", result=None, goals=[])

    assert ad.recover_abandoned_delegations() == 1
    event = _event(ad, "deleg_bare")

    assert event["status"] == "unknown"
    assert event["child_states"] == []
    assert "unknown" in event["error"].lower()


# ---------------------------------------------------------------------------
# Direction 2 — owner killed POST-DELIVERY: must be QUIET
# ---------------------------------------------------------------------------

def test_post_delivery_owner_exit_does_not_re_enter_conversation(ad):
    """A delivered result must never be resurrected as 'outcome unknown'."""
    _seed(
        ad,
        "deleg_delivered",
        delivery_state="delivered",
        result={"results": [{"status": "completed", "goal": "alpha"}]},
        goals=["alpha"],
    )

    ad.recover_abandoned_delegations()

    state, delivery_state, event_json, _ = _row(ad, "deleg_delivered")

    # THE regression: the row must not be flipped back to pending.
    assert delivery_state == "delivered"
    assert state not in ("running", "finalizing")  # settled, never re-reaped

    # And nothing is queued for restart recovery.
    import queue

    q = queue.Queue()
    ad.restore_undelivered_completions(q)
    assert q.empty()


def test_post_delivery_tombstone_is_idempotent(ad):
    """Re-running the reaper cannot resurrect a settled delivered row."""
    _seed(
        ad,
        "deleg_delivered2",
        delivery_state="delivered",
        result={"results": [{"status": "completed", "goal": "alpha"}]},
        goals=["alpha"],
    )
    ad.recover_abandoned_delegations()
    first = _row(ad, "deleg_delivered2")
    ad.recover_abandoned_delegations()
    assert _row(ad, "deleg_delivered2")[:2] == first[:2]


def test_delivered_tombstone_is_distinguishable_when_not_suppressed(ad, monkeypatch):
    """With suppression off the record still differs from a real loss."""
    monkeypatch.setattr(
        ad, "_suppress_delivered_tombstones", lambda: False, raising=False
    )
    _seed(
        ad,
        "deleg_delivered3",
        delivery_state="delivered",
        result={"results": [{"status": "completed", "goal": "alpha"}]},
        goals=["alpha"],
    )
    _seed(
        ad,
        "deleg_lost3",
        delivery_state="pending",
        result={"results": [{"status": "interrupted", "goal": "alpha"}]},
        goals=["alpha"],
    )

    assert ad.recover_abandoned_delegations() == 2

    delivered = _event(ad, "deleg_delivered3")
    lost = _event(ad, "deleg_lost3")

    # The contract: opposite situations produce distinguishable records.
    assert delivered["status"] != lost["status"]
    assert delivered["error"] != lost["error"]
    assert delivered["owner_exit_delivered"] is True
    assert lost["owner_exit_delivered"] is False
    assert delivered["unaccounted_children"] == []
    assert lost["unaccounted_children"] != []
    # Per-child state travels EITHER way.
    assert delivered["child_states"] and lost["child_states"]


# ---------------------------------------------------------------------------
# The bug class, stated as one invariant
# ---------------------------------------------------------------------------

def test_reaper_never_downgrades_a_delivered_row_to_pending(ad):
    """No reaper pass may move delivery_state from 'delivered' back to 'pending'.

    This is the whole bug class: delivery is tracked on a column orthogonal to
    ``state``, so any recovery path that keys only on ``state`` can clobber an
    already-delivered result.
    """
    for index in range(3):
        _seed(
            ad,
            f"deleg_d{index}",
            delivery_state="delivered",
            result={"results": [{"status": "completed", "goal": "g"}]},
            goals=["g"],
            state="running" if index % 2 == 0 else "finalizing",
        )

    for _ in range(3):
        ad.recover_abandoned_delegations()

    with ad._connect() as conn:
        downgraded = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE delivery_state='pending'"
        ).fetchone()[0]
    assert downgraded == 0


def test_config_default_gates_suppression_without_an_env_var():
    """The knob is a config.yaml setting; behavioral config is never an env var."""
    from hermes_cli.config import DEFAULT_CONFIG

    delegation = DEFAULT_CONFIG["delegation"]
    assert "suppress_delivered_owner_exit_tombstones" in delegation
    assert isinstance(delegation["suppress_delivered_owner_exit_tombstones"], bool)

    import tools.async_delegation as module

    # The module default must agree with the shipped config default, so the
    # two can never drift (read the live value, don't freeze a literal).
    assert (
        module._DEFAULT_SUPPRESS_DELIVERED_TOMBSTONES
        == delegation["suppress_delivered_owner_exit_tombstones"]
    )
