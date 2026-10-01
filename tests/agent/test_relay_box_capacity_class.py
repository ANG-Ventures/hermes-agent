"""claude-pool box-capacity / session-startup 503s classify as pool_pressure
and render a named cause, not "unclassified error" (t_0ff05041).

Samples are the three err_hash groups from the 7-day agent.log backfill
(t_a716610d): 25579a1bf2 n=162, b78f8f12d3 n=19, 73affe5337 n=3.
"""
from __future__ import annotations

import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp

SLOT = ("this box has no free interactive session slot; place the session on "
        "another box (6/6 slots in use)")
CLI_CHILDREN = ("this box (3.8 GB RAM) can run at most 4 CLI children concurrently "
                "and 3 are running or queued; its last 1 slot(s) are kept for "
                "conversations that already live here")
STARTUP = ("the interactive session on this box did not become ready before the "
           "startup deadline (the typed turn was not submitted)")

CASES = [
    (SLOT, fp.BOX_CAPACITY_CAUSE),
    (CLI_CHILDREN, fp.BOX_CAPACITY_CAUSE),
    (STARTUP, fp.BOX_STARTUP_CAUSE),
]


@pytest.mark.parametrize("text,cause", CASES)
def test_box_refusal_is_pool_pressure_with_named_cause(text, cause, tmp_path):
    assert fbe.classify_text(text, http_status=503) == "pool_pressure"
    # Through the full trigger path too (no relay header: text decides).
    assert fbe.classify_trigger(text=f"Error code: 503 - {text}", http_status=503) == (
        "pool_pressure", "text")
    row = {"trigger_class": "pool_pressure", "err_head": text, "http_status": 503,
           "from_provider": "claude-bpr", "hop": "relay"}
    assert fp._cause_phrase(row) == cause
    rider = fp.format_cause_rider(row)
    # The dead-letter floor keys on the cause falling to "unclassified error".
    assert "unclassified" not in rider
    assert rider.startswith(cause)
    # No floor branch rendered, so the dead-letter sentinel writes no row.
    _, floors = fp.cause_rider_with_floors(dict(row, ts=0))
    assert floors == ()
    ledger = tmp_path / "dead.jsonl"
    assert fbe.note_unclassified(row, rider, floors, path=ledger) is False
    assert not ledger.exists()


def test_startup_deadline_is_not_a_connection_timeout():
    # A relay that answered 503 is not a dropped connection; keep it out of conn.
    assert fbe.classify_text(STARTUP, exc_name="APITimeoutError") == "pool_pressure"
