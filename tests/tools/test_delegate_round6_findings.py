"""Prism P1s on ANG-Ventures/hermes-agent#1585 @ eeb1e221 (card t_c806b146).

- tools/delegate_tool.py:334 "Ambiguous Delivery" -- REAL. The ledger settled
  delivered text by substring, so an undelivered steer whose text sits inside
  a delivered batch ("o\\nb" inside "foo\\nbar") was settled in place of the
  steers that were delivered. RED on eeb1e221; green with the line-aligned
  exact tiling in ``_SteerLedger.deliver``.
- run_agent.py:486 "Teardown door swallows close failure" -- BY DESIGN. A
  parent close that hits a raising door must not fall back to a direct
  close (that is the I2 breach the door stops); the owning run's own
  ``_teardown(owner=True)`` still closes the child. Proof test below.
- tools/delegate_tool.py:297 "Unbounded steer ledger file growth" -- FALSE.
  Both ledger path shapes live in a directory the live-dir retention prune
  removes; ``prune_stale_live_dirs`` runs on every delegate_task dispatch.
"""
from __future__ import annotations

import json
import os
import threading
import time
import types


def _delivered_seqs(path):
    return [
        json.loads(line)["seq"]
        for line in path.read_text().splitlines()
        if json.loads(line)["op"] == "deliver"
    ]


# :334 Ambiguous Delivery -- REAL ---------------------------------------------
def test_f334_undelivered_substring_steer_is_not_settled_for_the_delivered_batch(tmp_path):
    from tools.delegate_tool import _SteerLedger

    led = _SteerLedger(tmp_path / "steer.jsonl")
    led.accept("o\nb")  # drained, never delivered
    led.accept("foo")
    led.accept("bar")
    led.deliver("foo\nbar")  # the agent's join of the two later steers
    assert led.missed() == "o\nb"
    assert _delivered_seqs(tmp_path / "steer.jsonl") == [1, 2]


def test_f334_multiline_steer_vs_batch_settles_the_delivered_entries(tmp_path):
    """Prism's own example: "foo\\nbar" drained, then "foo" and "bar" delivered."""
    from tools.delegate_tool import _SteerLedger

    led = _SteerLedger(tmp_path / "steer.jsonl")
    led.accept("foo\nbar")
    led.accept("foo")
    led.accept("bar")
    led.deliver("foo\nbar")
    assert _delivered_seqs(tmp_path / "steer.jsonl") == [1, 2]
    assert led.missed() == "foo\nbar"
    assert led.counts() == {"accepted": 1, "delivered": 2, "withdrawn": 0}


def test_f334_put_back_order_and_duplicates_are_tiled(tmp_path):
    from tools.delegate_tool import _SteerLedger

    led = _SteerLedger(tmp_path / "steer.jsonl")
    for t in ("a", "b", "a"):
        led.accept(t)
    led.deliver("b\na")  # newer text first, drained batch put back behind it
    assert led.missed() == "a"
    led.deliver("a")
    assert led.missed() is None


def test_f334_untileable_text_settles_only_line_aligned_entries(tmp_path):
    from tools.delegate_tool import _SteerLedger

    led = _SteerLedger(tmp_path / "steer.jsonl")
    led.accept("stop")
    led.accept("go")
    led.deliver("unrelated\nstop\nnonstop go")
    assert led.missed() == "go"


def test_f334_real_agent_drain_then_delivery_reports_the_dropped_steer(tmp_path):
    """End to end through the real AIAgent slot: steer, clear_interrupt drops
    it, two more steers are delivered by the real drain + delivery sink."""
    from agent.agent_runtime_helpers import note_steer_delivered
    from run_agent import AIAgent
    from tools.delegate_tool import _SteerLedger

    child = types.SimpleNamespace(_pending_steer=None, _pending_steer_lock=threading.Lock())
    for name in ("steer", "_drain_pending_steer"):
        setattr(child, name, types.MethodType(getattr(AIAgent, name), child))
    led = _SteerLedger.for_child(child, "sa-f334")
    led.path = tmp_path / "steer.jsonl"

    def accept(text):
        led.accept(text)
        assert child.steer(text)

    accept("o\nb")
    child._drain_pending_steer()  # dropped, like clear_interrupt / finalizer
    accept("foo")
    accept("bar")
    note_steer_delivered(child, child._drain_pending_steer())
    assert led.missed() == "o\nb"


# :486 Teardown door raising -- BY DESIGN -------------------------------------
def test_f486_raising_door_never_direct_closes_and_the_owner_run_still_closes(monkeypatch):
    import run_agent
    from tools import delegate_tool as dt

    closes = []
    child = types.SimpleNamespace(close=lambda: closes.append("close"), _subagent_id="sa-f486")
    dt._attach_owner_teardown(child)
    dt._hold_run(child)  # the child's run is live

    real = dt._teardown

    def flaky(c, reason, *, owner=False):
        if not owner:
            raise RuntimeError("door bug")
        return real(c, reason, owner=owner)

    monkeypatch.setattr(dt, "_teardown", flaky)
    # The parent's close/release loop: the door took the child, no fallback.
    assert run_agent._close_delegated_child(child, "parent_close") is True
    assert closes == []  # not closed under the live run
    # The owning run ends and closes it through the door.
    assert dt._teardown(child, "run_end", owner=True) is True
    assert closes == ["close"]


# :297 Steer ledger growth -- FALSE -------------------------------------------
def test_f297_both_ledger_path_shapes_are_removed_by_the_live_dir_prune(tmp_path, monkeypatch):
    from tools import delegation_live_log as dll
    from tools.delegate_tool import _SteerLedger

    root = tmp_path / "live"
    monkeypatch.setattr(dll, "live_transcript_root", lambda: root)

    beside = types.SimpleNamespace(_live_transcript_path=str(root / "deleg_x" / "task-0.log"))
    alone = types.SimpleNamespace()
    paths = []
    for child, sid in ((beside, "sa-a"), (alone, "sa-b")):
        led = _SteerLedger.for_child(child, sid)
        led.accept("steer")
        assert led.path.is_file()
        assert led.path.parent.parent == root  # a top-level live dir
        paths.append(led.path)

    old = time.time() - (dll.LIVE_RETENTION_DAYS + 1) * 86400
    for p in paths:
        os.utime(p.parent, (old, old))
    assert dll.prune_stale_live_dirs() == 2
    assert not any(p.exists() for p in paths)
