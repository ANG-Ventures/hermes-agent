"""Step 0 logging for the /stop held store (card t_1eb0a176): a line per item for hold, release and
clear, a WARNING when the held list shrinks with no line, and ``log_items`` no longer hiding its own
failure. Logging only: the last test proves the store ends up the same with the audit switched off.
"""
from __future__ import annotations

import logging

import pytest

from hermes_cli.queue_verbs import QueueOp
from tests.gateway.test_gateway_queue_mgmt import KEY, _active, _ev, _held, qm, rig  # noqa: F401  (rig is a fixture)


@pytest.fixture(autouse=True)
def _info(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="gateway.queue_mgmt")
    monkeypatch.setattr(qm, "_LOG_ITEMS_WARNED", False)


def _items(caplog, op):
    return [r.getMessage() for r in caplog.records if f"queue_mgmt item op={op} " in r.getMessage()]


def _shrinks(caplog):
    return [r for r in caplog.records if "op=unexplained_shrink" in r.getMessage()]


def _seed_ids(runner, adapter, *ids):
    events = [_ev(f"prompt {i}", message_id=i) for i in ids]
    assert qm.rewrite_active(runner, KEY, adapter, events)
    return events


def test_hold_release_and_clear_write_one_line_per_item(rig, caplog):
    runner, adapter = rig
    _seed_ids(runner, adapter, "11", "12")
    assert qm.hold_user_items(runner, KEY, adapter) == 2
    assert [m.split(" preview=")[0] for m in _items(caplog, "hold")] == [
        f"queue_mgmt item op=hold session={KEY} pos=1 msg_id=11 kind=user",
        f"queue_mgmt item op=hold session={KEY} pos=2 msg_id=12 kind=user"]
    assert qm.restage_held(runner, KEY, adapter) == 2
    assert [m.split(" preview=")[0].rsplit("msg_id=", 1)[1] for m in _items(caplog, "release")] == [
        "11 kind=user", "12 kind=user"]
    qm.hold_user_items(runner, KEY, adapter)
    qm.apply_management(runner, QueueOp("clear"), KEY, adapter)
    assert len(_items(caplog, "clear")) == 2
    assert _shrinks(caplog) == []   # every change above had its lines


def test_a_held_list_that_shrinks_with_no_line_is_reported(rig, caplog):
    runner, adapter = rig
    _seed_ids(runner, adapter, "21", "22")
    qm.hold_user_items(runner, KEY, adapter)
    del runner._session_state(KEY).conversation.held_queue[0]   # some path dropped an item silently
    qm.restage_held(runner, KEY, adapter)                        # the next logged change notices
    (rec,) = _shrinks(caplog)
    assert rec.levelno == logging.WARNING
    assert rec.getMessage() == f"queue_mgmt op=unexplained_shrink session={KEY} expected=2 found=1"
    assert _active(runner, adapter) == ["prompt 22"]            # and the release itself is untouched


def test_log_items_reports_its_own_failure_once_at_warning(rig, caplog, monkeypatch):
    runner, _adapter = rig
    monkeypatch.setattr(qm, "is_system_event", lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    qm.log_items(runner, "hold", KEY, [_ev("a", message_id="31")])
    qm.log_items(runner, "hold", KEY, [_ev("b", message_id="32")])
    failures = [r.levelno for r in caplog.records if "queue_mgmt item log failed" in r.getMessage()]
    assert failures == [logging.WARNING, logging.DEBUG]


def _sequence(runner, adapter):
    _seed_ids(runner, adapter, "41", "42", "43")
    qm.hold_user_items(runner, KEY, adapter)
    qm.apply_management(runner, QueueOp("rm", index=2), KEY, adapter)
    held_after_rm = _held(runner)
    released = qm.restage_held(runner, KEY, adapter)
    return held_after_rm, released, _active(runner, adapter), _held(runner)


def test_the_store_ends_up_the_same_with_the_audit_switched_off(rig, monkeypatch):
    runner, adapter = rig
    with_audit = _sequence(runner, adapter)
    qm.rewrite_active(runner, KEY, adapter, [])
    monkeypatch.setattr(qm, "held_audit", lambda *a, **k: None)
    assert _sequence(runner, adapter) == with_audit == (["prompt 41", "prompt 43"], 2, ["prompt 41", "prompt 43"], [])


def test_a_merged_held_release_logs_every_absorbed_message(rig, caplog):
    """Prism da666476e0b3: under ``queue_release_mode: merged`` the one turn carries the head's id
    only, so each absorbed message gets its own ``merged-into-head`` line naming the head."""
    caplog.set_level(logging.INFO, logger="gateway.inbox_log")
    runner, adapter = rig
    runner._queue_release_mode = "merged"
    _seed_ids(runner, adapter, "41", "42", "43")
    qm.hold_user_items(runner, KEY, adapter)
    assert qm.restage_held(runner, KEY, adapter) == 3
    lines = [r.getMessage() for r in caplog.records if r.name == "gateway.inbox_log"]
    got = [dict(p.split("=", 1) for p in m.split()[1:]) for m in lines]
    assert [(g["disposition"], g["msg_id"], g["path"], g["into"]) for g in got] == [
        ("merged-into-head", "42", "held_release_merged", "41"),
        ("merged-into-head", "43", "held_release_merged", "41")]
    assert all("prompt" not in m for m in lines)


def test_a_held_audit_that_raises_changes_no_outcome(rig):
    """Its depth record broken (``.get`` and ``.pop`` both fail): the store still ends up the same."""
    runner, adapter = rig
    with_audit = _sequence(runner, adapter)
    qm.rewrite_active(runner, KEY, adapter, [])
    runner.__dict__["_held_depth_seen"] = object()
    assert _sequence(runner, adapter) == with_audit
    # An automatic reset still carries the held items (a raise there would clear without carry).
    _seed_ids(runner, adapter, "51", "52")
    qm.hold_user_items(runner, KEY, adapter)
    carried = qm.before_scope_clear(runner, KEY, "auto_reset")
    assert [e.message_id for e in carried or ()] == ["51", "52"]
