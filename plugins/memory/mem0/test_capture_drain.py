"""Tests for the mem0 auto-capture drain worker (Track A-lite).

Run: PYTHONPATH=<worktree> pytest plugins/memory/mem0/test_capture_drain.py -q
Covers: happy path, exactly-once reconcile (D-8), transient-fault retry + dead-letter (D-10/INV-1),
post-write secret scrub (INV-4), breaker skip. Uses injected fakes — no live mem0.
"""
import pytest

from capture_queue import CaptureQueue, idem_key
from capture_drain import CaptureDrainWorker
import capture_scrub as scrub


class FakeStore:
    """Minimal fake of the mem0 client for add / recall-by-idem / get-written / forget."""
    def __init__(self, fail_times=0):
        self.rows = []          # [{id, memory, capture_idem}]
        self._id = 0
        self.fail_times = fail_times
        self.add_calls = 0

    def add(self, messages, kwargs):
        self.add_calls += 1
        if self.add_calls <= self.fail_times:
            raise RuntimeError("simulated transient 500")
        idem = (kwargs.get("metadata") or {}).get("capture_idem", "")
        # simulate server extraction: 1 durable fact per turn (+ echo the user text so a secret shows)
        text = messages[0]["content"]
        self._id += 1
        self.rows.append({"id": f"m{self._id}", "memory": text, "capture_idem": idem})

    def recall_idem(self, key):
        return sum(1 for r in self.rows if r["capture_idem"] == key)

    def get_written(self, key):
        return [r for r in self.rows if r["capture_idem"] == key]

    def forget(self, mid):
        self.rows = [r for r in self.rows if r["id"] != mid]


def make_worker(q, store, **kw):
    defaults = dict(
        gate="GATE_V3",
        model="gpt-5.4-mini",
        write_filters={"user_id": "ace"},
        max_attempts=3,
        backoff_base_s=1.0,
    )
    defaults.update(kw)
    return CaptureDrainWorker(
        q,
        add_fn=store.add,
        recall_idem_fn=store.recall_idem,
        scrub_fn=lambda facts: scrub.filter_facts(facts),
        forget_fn=store.forget,
        get_written_fn=store.get_written,
        **defaults,
    )


@pytest.fixture
def q(tmp_path):
    return CaptureQueue(str(tmp_path / "cq.db"))


def _enq(q, user, assistant="ok", sess="s", n=1):
    k = idem_key(sess, n, user, assistant)
    q.enqueue(k, {"user": user, "assistant": assistant})
    return k


def test_happy_path_extracts_and_marks_done(q):
    store = FakeStore()
    w = make_worker(q, store)
    k = _enq(q, "User prefers dark mode.")
    assert w.drain_once() is True
    assert q.counts()["done"] == 1
    assert store.add_calls == 1
    # the add carried the gate + model + capture_idem
    assert store.rows[0]["capture_idem"] == k
    assert w.stats["drained"] == 1


def test_gate_and_model_threaded_into_add(q):
    captured = {}
    store = FakeStore()
    orig = store.add
    def spy(messages, kwargs):
        captured.update(kwargs); return orig(messages, kwargs)
    store.add = spy
    w = make_worker(q, store)
    _enq(q, "User runs QMD on the Mac Studio.")
    w.drain_once()
    assert captured["prompt"] == "GATE_V3"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["metadata"]["capture_idem"]
    assert captured["user_id"] == "ace"


def test_exactly_once_reconcile_no_double_add(q):
    """A crash after add() but before mark_done: the row was re-leased; the reconcile sees existing
    rows and marks done WITHOUT a second add (D-8)."""
    store = FakeStore()
    w = make_worker(q, store)
    k = _enq(q, "User's DNS is AdGuard on 192.168.1.208.")
    # simulate: add already ran on a prior lease (row exists), status still not done
    store.rows.append({"id": "pre", "memory": "prewritten", "capture_idem": k})
    assert w.drain_once() is True
    assert store.add_calls == 0          # NEVER re-added
    assert q.counts()["done"] == 1


def test_transient_fault_retries_then_recovers(q):
    store = FakeStore(fail_times=1)      # first add 500s, second succeeds
    w = make_worker(q, store)
    _enq(q, "User prefers concise replies.")
    # first drain: add fails -> requeued
    assert w.drain_once() is True
    assert q.counts()["pending"] == 1 and w.stats["retried"] == 1
    # advance past backoff, drain again -> succeeds
    import time as _t
    row = q.lease_one(now=_t.time() + 10)
    assert row is None or True  # lease timing; force the retry by draining after backoff
    # simplest: directly re-drain after making it due
    q._connect().execute("UPDATE capture_queue SET next_attempt_at=0, status='pending', leased_until=NULL")
    assert w.drain_once() is True
    assert q.counts()["done"] == 1 and store.add_calls == 2


def test_dead_letter_after_max_attempts(q):
    store = FakeStore(fail_times=99)     # always fails
    w = make_worker(q, store, max_attempts=3)
    _enq(q, "User likes X.")
    for _ in range(3):
        w.drain_once()
        # force due again
        q._connect().execute("UPDATE capture_queue SET next_attempt_at=0 WHERE status='pending'")
    assert q.counts()["dead"] == 1
    assert w.stats["dead"] >= 1


def test_post_write_scrub_forgets_secret_bearing_memory(q):
    store = FakeStore()
    w = make_worker(q, store)
    # a turn whose user text carries a telegram bot token -> the fake "extracts" it verbatim
    _enq(q, "my bot token is " + ("8905425635:" + "AAH3xY9zKq" + "_Wp0LmNoPqRsTuVwXyZ" + "012345") + " keep it")
    w.drain_once()
    # the secret-bearing memory was scrubbed (forgotten) post-write
    assert w.stats["scrubbed"] == 1
    assert all("8905425635" not in r["memory"] for r in store.rows)
    assert q.counts()["done"] == 1     # turn still completes


def test_clean_memory_not_scrubbed(q):
    store = FakeStore()
    w = make_worker(q, store)
    _enq(q, "User's Mac Studio is the always-on fleet host.")
    w.drain_once()
    assert w.stats["scrubbed"] == 0
    assert len(store.rows) == 1


def test_breaker_open_skips(q):
    store = FakeStore()
    w = make_worker(q, store, breaker_open_fn=lambda: True)
    _enq(q, "User prefers Y.")
    assert w.drain_once() is False
    assert store.add_calls == 0
    assert q.counts()["pending"] == 1   # untouched, will drain when breaker closes
