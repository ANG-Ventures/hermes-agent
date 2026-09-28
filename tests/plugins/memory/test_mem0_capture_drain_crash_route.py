"""k122 (C7): a crash after the mem0 add but before Arm-B routing must not skip routing.

The next lease finds the rows already written and takes the exactly-once shortcut; that
path must still route the turn, or its world/event facts are never staged.
"""
from plugins.memory.mem0.capture_drain import CaptureDrainWorker
from plugins.memory.mem0.capture_queue import CaptureQueue, idem_key
from plugins.memory.mem0.capture_scrub import filter_facts


class _Store:
    def __init__(self):
        self.rows = []
        self.add_calls = 0

    def add(self, messages, kwargs):
        self.add_calls += 1
        idem = (kwargs.get("metadata") or {}).get("capture_idem", "")
        self.rows.append({"id": f"m{len(self.rows)}", "memory": messages[0]["content"],
                          "capture_idem": idem})
        return 1

    def recall_idem(self, key):
        return sum(1 for r in self.rows if r["capture_idem"] == key)

    def get_written(self, key):
        return [r for r in self.rows if r["capture_idem"] == key]

    def forget(self, mid):
        self.rows = [r for r in self.rows if r["id"] != mid]


class _SpyRouter:
    def __init__(self):
        self.calls = []

    def route_turn(self, user, assistant, *, turn_id, session, ts=None, profile=None):
        self.calls.append((user, turn_id, session))
        return {"error": None}


def test_shortcut_path_still_routes_a_turn_added_before_a_crash(tmp_path):
    q = CaptureQueue(str(tmp_path / "cq.db"))
    store = _Store()
    spy = _SpyRouter()
    w = CaptureDrainWorker(
        q, add_fn=store.add, recall_idem_fn=store.recall_idem,
        scrub_fn=filter_facts, forget_fn=store.forget,
        get_written_fn=store.get_written, gate="GATE_V3", model="m",
        write_filters={"user_id": "ace"}, max_attempts=3, backoff_base_s=1.0, router=spy)
    k = idem_key("s", 1, "Alex met Maria who runs a FinOps startup.", "cool")
    q.enqueue(k, {"user": "Alex met Maria who runs a FinOps startup.", "assistant": "cool",
                  "session_id": "sess-x"})
    # Prior lease: add committed, then the process died before routing / mark_done.
    store.add([{"role": "user", "content": "Alex met Maria"}], {"metadata": {"capture_idem": k}})

    assert w.drain_once() is True
    assert q.counts()["done"] == 1
    assert store.add_calls == 1          # exactly-once: no re-add
    assert len(spy.calls) == 1           # ...and the turn was still routed
    assert spy.calls[0][2] == "sess-x"
