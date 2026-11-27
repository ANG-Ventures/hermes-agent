"""Boomerang wake is decided at card birth, for every producer (Ace msg1569101606384050286).

Every task row is written by :func:`kanban_db.create_task`, a triage-decomposition insert
(``kanban_db_graph``) or a failure-owner mint (``kanban_failure_ownership._create_owner``), and
each settles ``boomerang_wake`` through :func:`resolve`, so this is the one rule. An
explicit value from the caller (``--no-boomerang-wake``, ``boomerang_wake: false``) is stored
as given; an unset value takes ``kanban.auto_subscribe_boomerang`` (default True). The card
column is the only setting: there is no second opt-in elsewhere.

The ``created`` event records ``boomerang_wake_source`` (``explicit`` | ``default`` |
``inherited``), so an
operator can tell an intentional opt-out from a producer that dropped the default. A drift
report should flag only a non-terminal card with NULL ``boomerang_wake``, or a False one whose
source is ``default`` while the policy is on.

The flag never invents a recipient. Delivery still needs a waker subscription row;
:func:`wake_route` reports whether one exists, and the create surfaces return it.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

SOURCE_EXPLICIT = "explicit"
SOURCE_DEFAULT = "default"
SOURCE_INHERITED = "inherited"   # a triage-decomposition child takes its root's stored value


def policy_default() -> bool:
    """``kanban.auto_subscribe_boomerang`` (default True). Unreadable config keeps the default."""
    try:
        from hermes_cli.config import cfg_get, load_config

        return cfg_get(load_config(), "kanban", "auto_subscribe_boomerang", default=True) is True
    except Exception:
        return True


def resolve(value: Optional[bool]) -> tuple[bool, str]:
    """``(stored value, source)`` for a create whose caller passed ``value``."""
    if value is not None:
        return bool(value), SOURCE_EXPLICIT
    return policy_default(), SOURCE_DEFAULT


def wake_route(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """How this card's wakes will actually be delivered, read back from the store.

    ``waker`` is ``platform:chat_id`` of the subscription that holds the wake, or None.
    ``note`` says so in words when the card is at boomerang level but nothing can be woken.
    """
    from hermes_cli import kanban_db_notify as kbn

    row = conn.execute("SELECT boomerang_wake FROM tasks WHERE id = ?", (task_id,)).fetchone()
    boomerang = bool(row[0]) if row and row[0] is not None else False
    subs = kbn.list_notify_subs(conn, task_id)
    waker = next((s for s in subs
                  if (s.get("delivery_mode") or "notify") in kbn.NOTIFY_WAKE_MODES), None)
    out: dict[str, Any] = {
        "level": kbn.card_level(boomerang, subs),
        "waker": f"{waker['platform']}:{waker['chat_id']}" if waker else None,
    }
    if boomerang and waker is None:
        out["note"] = ("boomerang wake is set but no chat holds the wake: nothing is woken "
                       "until one subscribes (kanban notify-subscribe)")
    return out
