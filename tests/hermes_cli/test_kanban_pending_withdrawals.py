"""Auth-recovery regression through the real alloc_client and loopback door."""
import json
import logging
import time

import pytest

from tests.hermes_cli.test_kanban_door_round5 import door_fleet, fleet, POOL, _card, _uid
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_priority as priority
from hermes_cli import kanban_priority_door as door
from hermes_cli import kanban_priority_faults as faults
from hermes_cli.fleet_placement import alloc_client, admission_demand


def _pending(conn):
    return json.loads(door._published_path(conn).read_text()).get("pending_withdrawals", [])


def test_pair_token_recovery_retries_durable_withdrawal(door_fleet, caplog):
    tid = _card()
    uid = _uid(tid)
    token = door_fleet.root / alloc_client.TOKENS_REL / f"kanban@{POOL}"
    saved = token.read_bytes()
    caplog.set_level(logging.WARNING, logger=priority.__name__)
    with kbc.connect_closing() as conn:
        assert priority.publish_waiting(conn, {POOL: [{"id": tid, "asap": False}]}) == {}
        assert uid in door_fleet.allocd.waiting(POOL)
        token.unlink()
        assert priority.publish_waiting(conn, {}) == {}
        assert faults.latched() is None
        assert door.load_published(conn) == set()
        pending = _pending(conn)
        assert [(p["host"], p["unit_id"]) for p in pending] == [(POOL, uid)]
        assert pending[0]["first_seen"] > 0 and pending[0]["last_reason"]
        warnings = len(caplog.records)
        assert priority.publish_waiting(conn, {}) == {}
        assert len(caplog.records) == warnings
        assert faults.latched() is None
        # Another writer must not erase the pending list.
        door.save_published(conn, door.load_published(conn))
    token.write_bytes(saved)
    with kbc.connect_closing() as conn:
        assert priority.publish_waiting(conn, {}) == {}
        assert uid not in door_fleet.allocd.waiting(POOL)
        assert _pending(conn) == []


def test_refreshed_remote_unit_outlives_local_pending_age(door_fleet, monkeypatch):
    tid = _card()
    uid = _uid(tid)
    token = door_fleet.root / alloc_client.TOKENS_REL / f"kanban@{POOL}"
    saved = token.read_bytes()
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    with kbc.connect_closing() as conn:
        assert priority.publish_waiting(conn, {POOL: [{"id": tid, "asap": False}]}) == {}
        token.unlink()
        assert priority.publish_waiting(conn, {}) == {}
        pending = _pending(conn)
        assert len(pending) == 1
        # Another publisher refreshes the remote unit, not this board's pending timestamp.
        ttl = admission_demand.max_age_s()
        clock[0] += ttl * 0.75
        token.write_bytes(saved)
        alloc_client.enqueue("kanban", POOL, uid, 1, 3)
        token.unlink()
        clock[0] += ttl * 0.5
        assert clock[0] - door_fleet.allocd.waiting(POOL)[uid]["seen_at"] < ttl
        assert priority.publish_waiting(conn, {}) == {}
        assert _pending(conn) == pending
        token.write_bytes(saved)
        assert priority.publish_waiting(conn, {}) == {}
        assert uid not in door_fleet.allocd.waiting(POOL)
        assert _pending(conn) == []


def test_publish_wanted_again_preserves_arrival_and_urgency(door_fleet, monkeypatch):
    tid = _card()
    uid = _uid(tid)
    (door_fleet.root / alloc_client.TOKENS_REL / alloc_client.ASAP_OPERATOR).write_text("asap\n", encoding="utf-8")
    token = door_fleet.root / alloc_client.TOKENS_REL / f"kanban@{POOL}"
    saved = token.read_bytes()
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    wanted = {POOL: [{"id": tid, "asap": True}]}
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET asap=1 WHERE id=?", (tid,))
        conn.commit()
        assert priority.publish_waiting(conn, wanted) == {}
        original = door_fleet.allocd.waiting(POOL)[uid]
        assert original["asap"] is True
        token.unlink()
        assert priority.publish_waiting(conn, {}) == {}
        assert len(_pending(conn)) == 1
        token.write_bytes(saved)
        clock[0] += 1
        start = len(door_fleet.allocd.calls)
        assert priority.publish_waiting(conn, wanted) == {}
        withdraws = [body for path, body in door_fleet.allocd.calls[start:]
                     if path == "/demand/publish" and body.get("op") == "withdraw"]
        assert withdraws == []
        renewed = door_fleet.allocd.waiting(POOL)[uid]
        assert renewed["at"] == original["at"]
        assert renewed["asap"] is True
        assert _pending(conn) == []
        assert door.load_published(conn) == {(POOL, uid)}


def test_enqueue_again_cancels_obsolete_pending_withdrawal(door_fleet):
    tid = _card()
    uid = _uid(tid)
    with kbc.connect_closing() as conn:
        door.save_published(conn, set(), pending_withdrawals=[{
            "host": POOL, "unit_id": uid, "first_seen": 100, "last_reason": "auth"}])
        door.enqueue_here(alloc_client, conn, POOL, uid)
        assert _pending(conn) == []
        assert priority.publish_waiting(conn, {POOL: [{"id": tid, "asap": False}]}) == {}
        assert uid in door_fleet.allocd.waiting(POOL)


@pytest.mark.parametrize("reason", ["auth", "invalid", "transport", "bare", "absent"])
def test_pending_retry_preserves_fault_taxonomy(door_fleet, monkeypatch, reason):
    with kbc.connect_closing() as conn:
        door.save_published(conn, set(), pending_withdrawals=[{
            "host": POOL, "unit_id": "pending-id", "first_seen": 100, "last_reason": "auth"}])
        monkeypatch.setattr("time.time", lambda: 100)
        errors = {"auth": alloc_client.AuthorizeRefused("auth"),
                  "invalid": alloc_client.AuthorizeRefused("invalid"),
                  "transport": alloc_client.Unreachable("network"),
                  "bare": alloc_client.Refused("unknown")}
        if reason in errors:
            def refused(*args):
                raise errors[reason]
            monkeypatch.setattr(alloc_client, "withdraw", refused)
        result = priority.publish_waiting(conn, {})
        assert bool(result) == (reason in ("invalid", "transport", "bare"))
        assert bool(faults.latched()) == (reason in ("invalid", "bare"))
        assert len(_pending(conn)) == (0 if reason == "absent" else 1)
        if reason != "absent":
            assert _pending(conn)[0]["first_seen"] == 100
        with faults._state() as doc:
            assert ("publish" in faults._transport(doc)) == (reason == "transport")


def test_real_http404_is_not_auth_and_keeps_record(door_fleet, monkeypatch):
    uid = "http404-unit"
    monkeypatch.setattr(door_fleet.allocd, "route", lambda *a: (404, {"error": "missing endpoint"}))
    with kbc.connect_closing() as conn:
        door.save_published(conn, {(POOL, uid)})
        assert priority.publish_waiting(conn, {})
        assert door.load_published(conn) == {(POOL, uid)}
        assert faults.latched()
        assert _pending(conn) == []
