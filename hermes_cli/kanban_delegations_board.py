"""The internal ``delegations`` board (option C, docs/dev/delegate-background-on-kanban-runtime.md §6).

One background ``delegate_task`` unit = one card on this board. Its behaviour differs from every
other board in four places, all keyed on the slug constant below and never on ``board.json``
(a writable file must not switch a board to a different runner or respawn policy, RD-9):

* K1 the worker argv is the delegation-unit runner (``kanban_db_dispatch._default_spawn``);
* K2 a run that ends without its own terminal call parks the card ``blocked``, never ``ready``
  (no automatic re-run, I3, Ace 2026-10-21 Q1 = a);
* K4 its running cards are not counted against kanban capacity (Q2 = a);
* K5 ``create_task`` skips the same-incident overlap gate.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

DELEGATIONS_BOARD = "delegations"

#: Status a delegations card takes where another board's card would be requeued.
PARKED_STATUS = "blocked"


def is_delegations_board(board: Optional[str]) -> bool:
    """``board`` names the delegations board (slug compare, after normalization)."""
    if not board:
        return False
    from hermes_cli import kanban_db as _kb

    try:
        return _kb._normalize_board_slug(board) == DELEGATIONS_BOARD
    except Exception:
        return False


def conn_is_delegations(conn: sqlite3.Connection) -> bool:
    """*conn* is open on the delegations board's DB file (the file decides, not the caller)."""
    from hermes_cli import kanban_db as _kb

    return _kb._conn_is_board(conn, DELEGATIONS_BOARD)


def requeue_status(conn: sqlite3.Connection, retry_status: str) -> str:
    """K2: the status a released running card takes. ``retry_status`` elsewhere, ``blocked`` here."""
    return PARKED_STATUS if conn_is_delegations(conn) else retry_status


def park_event(conn: sqlite3.Connection, task_id: str, *, outcome: str,
               retry_status: str, run_id: Optional[int] = None) -> None:
    """The ``blocked`` event that makes a K2 park sticky (``recompute_ready`` never promotes it).

    Call inside the caller's write txn, right after the UPDATE that set ``blocked``. The original
    outcome rides the payload so the board says why the unit stopped.
    """
    from hermes_cli import kanban_db as _kb

    _kb._append_event(conn, task_id, "blocked", {
        "reason": f"{DELEGATIONS_BOARD} board: run ended ({outcome}); no automatic re-run",
        "outcome": outcome,
        "source_status": retry_status,
        "board": DELEGATIONS_BOARD,
        "auto": True,
    }, run_id=run_id)
