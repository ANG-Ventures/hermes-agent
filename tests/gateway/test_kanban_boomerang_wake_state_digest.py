"""t_2f7fd11c: the boomerang wake state file keeps a digest of the assessed card text, not the
text, and is not rewritten when nothing in it changed.

Measured 2026-11-13: the Studio file was 25,966,905 B with 5,732 claims; ``assessed_card_text``
was 15,726,301 B of it (61%), and every state change rewrote the whole file.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from gateway import kanban_boomerang_wake as ah
from tests.gateway.test_kanban_boomerang_wake_backoff import T0, Clock, _row
from tests.gateway.test_kanban_boomerang_wake_dispatch import (  # noqa: F401 (fixture)
    _card, _run, _sub, _tick, _verdict, env,
)
from hermes_cli import kanban_db as kb

RETRY = {"evidence": ["health probe: healthy"],
         "block_resolution": {"resolved": True, "authority": "existing", "check": "health probe: healthy"}}


def _blocked_claim(env, body: str = "needs the probe") -> tuple:  # noqa: F811
    tid = _card(env, title="digest card", body=body)
    _sub(tid)
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "blocked", {"kind": "transient", "reason": "service unavailable"})  # noqa: SLF001
    _tick(env)
    return tid, env["dispatched"][0]


def _card_text(tid: str) -> tuple:
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        return task.title, task.body


def _status(tid: str) -> str:
    with kb.connect_closing() as conn:
        return kb.get_task(conn, tid).status


def _as_legacy(entry: dict, title: str, body: str) -> dict:
    """The claim as a pre-digest gateway wrote it: the text, no digest (on disk and in memory)."""
    state = ah.BoomerangWakeState()
    data = json.loads(state.path.read_text())
    claim = data["claims"][entry["key"]]
    claim.pop("assessed_card_digest", None)
    claim["assessed_card_text"] = [title, body]
    state.path.write_text(json.dumps(data, sort_keys=True))
    legacy = {k: v for k, v in entry.items() if k != "assessed_card_digest"}
    legacy["assessed_card_text"] = [title, body]
    return legacy


def _file_id(path) -> tuple:
    st = path.stat()
    return st.st_ino, st.st_mtime_ns, st.st_size


def test_claim_stores_the_digest_not_the_card_text(env):  # noqa: F811
    body = "a long body " * 200
    tid, entry = _blocked_claim(env, body=body)
    raw = ah.BoomerangWakeState().path.read_text()
    assert body not in raw and "assessed_card_text" not in raw
    claim = ah.BoomerangWakeState().get_claim(entry["key"])
    assert claim["assessed_card_digest"] == kb.card_text_digest(*_card_text(tid))


def test_unchanged_card_is_unblocked_by_the_digest(env):  # noqa: F811
    tid, entry = _blocked_claim(env)
    _run(env, entry, "completed", _verdict(entry, "retry", **RETRY))
    assert _status(tid) != "blocked"


@pytest.mark.parametrize("field", ["title", "body"])
def test_changed_card_is_seen_as_changed_by_the_digest(env, field):  # noqa: F811
    tid, entry = _blocked_claim(env)
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {field} = 'different prerequisite' WHERE id = ?", (tid,))
    _run(env, entry, "completed", _verdict(entry, "retry", **RETRY))
    assert _status(tid) == "blocked"


@pytest.mark.parametrize("changed", [False, True])
def test_legacy_text_claim_is_read_and_converted_on_next_write(env, changed):  # noqa: F811
    tid, entry = _blocked_claim(env)
    legacy = _as_legacy(entry, *_card_text(tid))
    state = ah.BoomerangWakeState()
    # read without error, already as a digest
    assert state.get_claim(entry["key"])["assessed_card_digest"] == kb.card_text_digest(*_card_text(tid))
    assert "assessed_card_text" in state.path.read_text()          # nothing written by a read
    if changed:
        with kb.connect_closing() as conn, kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = 'different prerequisite' WHERE id = ?", (tid,))
    _run(env, legacy, "completed", _verdict(legacy, "retry", **RETRY))
    assert (_status(tid) == "blocked") is changed
    raw = state.path.read_text()
    arch = state.archive_path.read_text() if state.archive_path.exists() else ""
    assert "assessed_card_text" not in raw + arch
    # t_e33a5355: the claim is terminal now, so it lives in the archive without its card digest
    assert entry["key"] not in json.loads(raw).get("claims", {}) and entry["key"] in arch


def test_no_rewrite_over_ten_ticks_without_a_new_event(env, monkeypatch):  # noqa: F811
    tid, entry = _blocked_claim(env)
    _run(env, entry, "completed", _verdict(entry, "retry", **RETRY))   # a terminal claim on file
    clock = Clock()
    monkeypatch.setattr(ah, "_time", type("T", (), {"time": staticmethod(clock.time)})())
    monkeypatch.setattr(ah, "_LAST_PRUNE", {}, raising=False)
    monkeypatch.setattr(ah, "BACKOFF_BASE_S", 10.0, raising=False)
    state = ah.BoomerangWakeState()
    row = _row(tid, parts=[ah.PART_LINE], created_at=T0)
    row.update(attempts=6, last_attempt_at=T0)          # failed 6x: next retry is 320 s out
    state.put_handback("del-wait", row)
    before = _file_id(state.path)
    for _ in range(10):
        asyncio.run(ah.reconcile(env["runner"], now=clock.now))
        state.update_claim(entry["key"], state=ah.STATE_TERMINAL)       # same value: a no-op
        state.drop_handback("del-unknown")
        clock.now += 5.0
    assert _file_id(state.path) == before
    assert state.handbacks()["del-wait"]["attempts"] == 6


def test_a_real_change_is_still_written(env):  # noqa: F811
    tid, entry = _blocked_claim(env)
    state = ah.BoomerangWakeState()
    before = _file_id(state.path)
    state.update_claim(entry["key"], child_session="s-1")
    assert _file_id(state.path) != before
    assert ah.BoomerangWakeState().get_claim(entry["key"])["child_session"] == "s-1"
