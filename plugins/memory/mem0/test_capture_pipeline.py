"""Tests for the mem0 auto-capture pipeline orchestration (Track A-lite, A3).

Covers: gate-version guard (D-11), cross-process bgr interlock (D-7), active-gating
(off/uncertified => inert), enqueue degrade-safety (INV-3). Uses injected fakes.
Run: PYTHONPATH=<worktree> pytest plugins/memory/mem0/test_capture_pipeline.py -q
"""
import os
import pytest

import capture_pipeline as cp
import capture_scrub as scrub
from capture_queue import idem_key


class FakeStore:
    def __init__(self):
        self.rows = []
        self._id = 0
    def add(self, messages, kwargs):
        idem = (kwargs.get("metadata") or {}).get("capture_idem", "")
        self._id += 1
        self.rows.append({"id": f"m{self._id}", "memory": messages[0]["content"], "capture_idem": idem})
    def recall_idem(self, k): return sum(1 for r in self.rows if r["capture_idem"] == k)
    def get_written(self, k): return [r for r in self.rows if r["capture_idem"] == k]
    def forget(self, mid): self.rows = [r for r in self.rows if r["id"] != mid]


def make_pipeline(tmp_path, store, capture_on=True, expected_version=None, **kw):
    return cp.CapturePipeline(
        capture_on_fn=lambda: capture_on() if callable(capture_on) else capture_on,
        add_fn=store.add,
        recall_idem_fn=store.recall_idem,
        scrub_fn=lambda facts: scrub.filter_facts(facts),
        forget_fn=store.forget,
        get_written_fn=store.get_written,
        write_filters={"user_id": "ace"},
        model="gpt-5.4-mini",
        queue_path=str(tmp_path / "cq.db"),
        expected_gate_version=expected_version,
        **kw,
    )


# ---- gate loads from the staged assets (v3 certified) ----------------------
def test_certified_gate_loads():
    gate, version = cp.load_certified_gate()
    assert gate.strip(), "certified gate string should load from assets/capture_gate_v3.txt"
    assert version.startswith("v3:"), f"expected v3 gate version, got {version!r}"


# ---- D-11 gate-version guard -----------------------------------------------
def test_active_when_certified_and_on(tmp_path):
    p = make_pipeline(tmp_path, FakeStore(), capture_on=True)
    assert p._certified is True
    assert p.active is True


def test_inert_when_capture_off(tmp_path):
    p = make_pipeline(tmp_path, FakeStore(), capture_on=False)
    assert p.active is False
    assert p.enqueue_turn("User prefers dark mode.", "ok", session_id="s", turn_ordinal=1) is False


def test_paused_on_gate_version_mismatch(tmp_path):
    # expected version pinned to a WRONG value => not certified => capture disabled (D-11)
    alerts = []
    p = make_pipeline(tmp_path, FakeStore(), capture_on=True,
                      expected_version="v3:DEADBEEF0000", alert_fn=alerts.append)
    assert p._certified is False
    assert p.active is False
    assert any("gate version mismatch" in a for a in alerts)
    assert p.enqueue_turn("x durable fact", "ok") is False   # never writes through an uncertified gate


# ---- cross-process interlock (D-7) -----------------------------------------
def test_bgr_write_blocked_when_capture_on():
    # foreground auto-capture ON => bgr writer must self-disable
    assert cp.bgr_write_allowed(capture_is_on=True) is False
    # capture OFF => bgr may write
    assert cp.bgr_write_allowed(capture_is_on=False) is True


# ---- enqueue is the durability boundary, degrade-safe (INV-3) --------------
def test_enqueue_then_drain_e2e(tmp_path):
    store = FakeStore()
    p = make_pipeline(tmp_path, store, capture_on=True)
    assert p.enqueue_turn("User's DNS is AdGuard on 192.168.1.208.", "noted",
                          session_id="s", turn_ordinal=1) is True
    # the worker started lazily on first enqueue; drain one synchronously to assert e2e
    p._worker.drain_once()
    assert len(store.rows) == 1
    assert store.rows[0]["capture_idem"] == idem_key("s", 1, "User's DNS is AdGuard on 192.168.1.208.", "noted")
    p.stop()


def test_duplicate_turn_enqueue_is_noop(tmp_path):
    store = FakeStore()
    p = make_pipeline(tmp_path, store, capture_on=True)
    p.enqueue_turn("same turn", "ok", session_id="s", turn_ordinal=1)
    assert p.enqueue_turn("same turn", "ok", session_id="s", turn_ordinal=1) is False  # idem dup
    p.stop()


def test_worker_starts_even_when_enqueue_is_duplicate(tmp_path):
    """Greptile P1: after a restart with rows already in SQLite, the next enqueue may be a duplicate
    (returns False). The drain/reaper worker must still START (there is durable work to drain), not
    stay idle until some later unique turn."""
    store = FakeStore()
    p = make_pipeline(tmp_path, store, capture_on=True)
    # pre-seed a pending row directly in the queue (simulating a prior process's un-drained work)
    p._queue.enqueue(idem_key("s", 1, "prior fact", "ok"),
                     {"user": "prior fact", "assistant": "ok"})
    assert p._started is False
    # a DUPLICATE enqueue (same key) returns False, but must still spin up the worker
    got = p.enqueue_turn("prior fact", "ok", session_id="s", turn_ordinal=1)
    assert got is False                 # duplicate
    assert p._started is True           # ...yet the worker started so the pending row drains
    p.stop()


def test_live_capture_flip_honored(tmp_path):
    """capture_on is read fresh each call — an off->on flip mid-process is honored (no restart)."""
    state = {"on": False}
    p = make_pipeline(tmp_path, FakeStore(), capture_on=lambda: state["on"])
    assert p.enqueue_turn("fact A", "ok", session_id="s", turn_ordinal=1) is False  # off
    state["on"] = True
    assert p.enqueue_turn("fact B", "ok", session_id="s", turn_ordinal=2) is True   # flipped on, no restart
    p.stop()
