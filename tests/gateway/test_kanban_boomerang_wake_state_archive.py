"""t_e33a5355: terminal boomerang-wake claims leave the hot state file for an append-only archive.

Measured on the Mac Studio 2026-11-16/17: ``kanban_boomerang_wake.json`` was 11,871,994 B with 6,281
claims, 6,276 of them terminal, and every state change rewrote all of it into a ``.boomerang-wake-*``
temp (228 temps in 49 min). A write now carries the open claims, the counters and the handback queue;
a terminal claim is one appended line with only ``TERMINAL_FIELDS``.
"""
from __future__ import annotations

import json
import os

from gateway import kanban_boomerang_wake as ah

T = ah.STATE_TERMINAL


def _full(key: str, i: int = 0, **kw) -> dict:
    """A claim the size the Studio's were (~1.9 KB: wake_text, route, digest ...)."""
    tid = key.split("|")[1]
    entry = {
        "key": key, "task_id": tid, "board": "default", "event_id": 1000 + i, "kind": "blocked",
        "row": "blocked_assess", "state": "claimed", "claimed_at": 1794000000.0 + i, "event_ts": 1794000000,
        "delegation_id": f"del-{i:06d}", "title": "a card title " * 4, "wake_text": "wake text " * 70,
        "route": {"chat_id": "1569184382705602631", "platform": "discord", "profile": None,
                  "thread_id": "1569184382705602631"},
        "assessed_card_digest": "sha256:" + "0" * 64, "assessed_last_comment": 333925,
        "home_sid": "20261108_190912_eb9284ca", "verdict_nonce": "41389d3ad8b053ec", "mode": "fork",
        "max_minutes": 45, "path": "owner-wake", "assignee": "daedalus", "pr": None, "head": None,
        "status": "blocked", "cursor_event_id": 1000 + i, "subagent_id": None, "child_session": None,
    }
    entry.update(kw)
    return entry


def _key(i: int) -> str:
    return f"default|t_{i:08x}|{1000 + i}"


def _hot(state) -> dict:
    return json.loads(state.path.read_text())


def test_terminal_claim_moves_to_the_archive_with_only_terminal_fields(tmp_path):
    state = ah.BoomerangWakeState(tmp_path / "gateway" / ah.STATE_FILENAME)
    assert state.claim(_key(1), _full(_key(1), 1))
    assert _key(1) in _hot(state)["claims"]                         # open: hot, full
    state.update_claim(_key(1), state=T, outcome="surface", finished_at=1794000100.0)
    assert _key(1) not in _hot(state)["claims"]
    (rec,) = [json.loads(line) for line in state.archive_path.read_text().splitlines()]
    assert set(rec) <= ah.TERMINAL_FIELDS and "wake_text" not in rec and "route" not in rec
    assert rec["outcome"] == "surface" and rec["delegation_id"] == "del-000001"
    # every reader still sees it
    assert state.has_claim(_key(1)) and state.get_claim(_key(1))["state"] == T
    assert _key(1) in state.claims()
    assert not state.claim(_key(1), _full(_key(1), 1))              # dedupe holds across the move


def test_a_claim_a_pending_handback_names_stays_hot_until_the_handback_drops(tmp_path):
    state = ah.BoomerangWakeState(tmp_path / ah.STATE_FILENAME)
    state.claim(_key(2), _full(_key(2), 2))
    state.update_claim(_key(2), state=T, outcome="surfaced",
                       handback=("del-000002", {"task_id": "t_2", "claim_key": _key(2), "parts": ["line"]}))
    assert _hot(state)["claims"][_key(2)]["wake_text"]              # the delivery guard reads the full claim
    assert not state.archive_path.exists()
    state.update_claim(_key(2), resurface_delivered=True)
    state.drop_handback("del-000002")
    assert _key(2) not in _hot(state)["claims"]
    assert state.get_claim(_key(2))["resurface_delivered"] is True


def test_update_of_an_archived_claim_appends_one_line_and_reads_back(tmp_path):
    state = ah.BoomerangWakeState(tmp_path / ah.STATE_FILENAME)
    state.claim(_key(3), _full(_key(3), 3))
    state.update_claim(_key(3), state=T, outcome="surfaced", resurface_delivered=False)
    state.update_claim(_key(3), resurface_delivered=True)
    assert len(state.archive_path.read_text().splitlines()) == 2
    assert state.get_claim(_key(3))["resurface_delivered"] is True
    hot_before = state.path.stat().st_mtime_ns
    state.update_claim(_key(3), resurface_delivered=True)           # same value: no write anywhere
    assert len(state.archive_path.read_text().splitlines()) == 2
    assert state.path.stat().st_mtime_ns == hot_before


def test_legacy_file_with_thousands_of_terminal_claims_shrinks_on_the_next_write(tmp_path, monkeypatch):
    path = tmp_path / ah.STATE_FILENAME
    claims = {_key(i): _full(_key(i), i, state=T, outcome="surface") for i in range(3000)}
    path.write_text(json.dumps({"claims": claims, "retries": {}, "restart_spent": {}, "blocked_spent": {},
                                "handbacks": {}, "dead_letters": {}}, sort_keys=True))
    legacy_bytes = path.stat().st_size
    written = []
    real_replace = os.replace

    def spy(src, dst):
        written.append(os.path.getsize(src))
        return real_replace(src, dst)

    monkeypatch.setattr(ah.os, "replace", spy)
    state = ah.BoomerangWakeState(path)
    assert state.claim("default|t_new|1", _full("default|t_new|1", 99999))   # one write migrates
    after = path.stat().st_size
    assert legacy_bytes > 4_000_000 and after < 10_000, (legacy_bytes, after)
    state.update_claim("default|t_new|1", state=ah.STATE_RUNNING)
    state.update_claim("default|t_new|1", state=T, outcome="surface")
    assert max(written) < 100_000, written                          # card target: < 100 KB per wake write
    assert len(state.claims()) == 3001 and state.has_claim(_key(0))


def test_prune_rewrites_the_archive_without_the_dropped_claims(tmp_path):
    state = ah.BoomerangWakeState(tmp_path / ah.STATE_FILENAME)
    for i, at in ((4, 1000.0), (5, 9000.0)):
        state.claim(_key(i), _full(_key(i), i))
        state.update_claim(_key(i), state=T, outcome="surface", finished_at=at)
    assert state.prune_claims(now=10000.0, ttl_s=5000.0) == 1
    lines = state.archive_path.read_text().splitlines()
    assert [json.loads(line)["key"] for line in lines] == [_key(5)]
    assert not state.has_claim(_key(4)) and state.has_claim(_key(5))


def test_torn_archive_line_is_skipped_and_hot_copy_wins_after_a_crash(tmp_path):
    state = ah.BoomerangWakeState(tmp_path / ah.STATE_FILENAME)
    state.claim(_key(6), _full(_key(6), 6))
    state.update_claim(_key(6), state=T, outcome="surface")
    with open(state.archive_path, "a") as fh:
        fh.write('{"key": "default|t_torn')                          # killed mid-append
    # crash after the append, before the hot replace: the archive says terminal, the hot file says open
    with open(state.archive_path, "a") as fh:
        fh.write("\n" + json.dumps({"key": _key(7), "state": T, "task_id": "t_7"}) + "\n")
    data = _hot(state)
    data["claims"][_key(7)] = _full(_key(7), 7, state=ah.STATE_RUNNING)
    state.path.write_text(json.dumps(data))
    assert state.get_claim(_key(6))["state"] == T
    assert state.get_claim(_key(7))["state"] == ah.STATE_RUNNING
    assert len(state.claims()) == 2


def test_sweep_removes_dead_temps_only(tmp_path):
    gw = tmp_path / "gateway"
    gw.mkdir()
    state = ah.BoomerangWakeState(gw / ah.STATE_FILENAME)
    old, fresh, other = gw / ".boomerang-wake-abc", gw / ".boomerang-wake-def", gw / "keep.json"
    for p in (old, fresh, other):
        p.write_bytes(b"x" * 10)
    os.utime(old, (1000.0, 1000.0))
    os.utime(other, (1000.0, 1000.0))
    os.utime(fresh, (5000.0, 5000.0))
    assert state.sweep_temps(now=5100.0) == 1
    assert not old.exists() and fresh.exists() and other.exists()


def test_first_reconcile_tick_sweeps_leftover_temps(tmp_path, monkeypatch):
    import asyncio
    import time

    monkeypatch.setattr(ah, "_LAST_PRUNE", {})
    state = ah.BoomerangWakeState(tmp_path / ah.STATE_FILENAME)
    state.claim(_key(8), _full(_key(8), 8))
    left = tmp_path / ".boomerang-wake-q2_u9gsw"
    left.write_bytes(b"x" * 1000)
    os.utime(left, (time.time() - 3600, time.time() - 3600))
    asyncio.run(ah._maybe_prune_claims(state, time.time()))   # noqa: SLF001
    assert not left.exists() and state.path.exists()
