"""Step 0 logging for the /stop held store (card t_1eb0a176): a line per item for hold, release and
clear, and one ``merged-into-head`` line per message a merged release absorbs.
"""
from __future__ import annotations

import logging

import pytest

from hermes_cli.queue_verbs import QueueOp
from tests.gateway.test_gateway_queue_mgmt import KEY, _ev, qm, rig  # noqa: F401  (rig is a fixture)


@pytest.fixture(autouse=True)
def _info(caplog):
    caplog.set_level(logging.DEBUG, logger="gateway.queue_mgmt")


def _items(caplog, op):
    return [r.getMessage() for r in caplog.records if f"queue_mgmt item op={op} " in r.getMessage()]


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
