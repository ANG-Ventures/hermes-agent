"""Thread-lifecycle regressions for the Mem0 provider."""

import threading
import time
from unittest.mock import MagicMock

from plugins.memory.mem0 import Mem0MemoryProvider
from plugins.memory.mem0.capture_pipeline import CapturePipeline
from plugins.memory.mem0.capture_scrub import filter_facts


class _FakeStore:
    def __init__(self):
        self.rows = []

    def add(self, messages, kwargs):
        idem = (kwargs.get("metadata") or {}).get("capture_idem", "")
        self.rows.append({"id": str(len(self.rows) + 1), "capture_idem": idem})
        return 1

    def recall_idem(self, key):
        return sum(row["capture_idem"] == key for row in self.rows)

    def get_written(self, key):
        return [row for row in self.rows if row["capture_idem"] == key]

    def forget(self, memory_id):
        self.rows = [row for row in self.rows if row["id"] != memory_id]


class _BlockingStore(_FakeStore):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def add(self, messages, kwargs):
        self.started.set()
        assert self.release.wait(timeout=5.0)
        return super().add(messages, kwargs)


def _capture_pipeline(tmp_path, store):
    return CapturePipeline(
        capture_on_fn=lambda: True,
        add_fn=store.add,
        recall_idem_fn=store.recall_idem,
        scrub_fn=filter_facts,
        forget_fn=store.forget,
        get_written_fn=store.get_written,
        write_filters={"user_id": "ace"},
        model="test",
        queue_path=str(tmp_path / "capture.db"),
    )


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_shutdown_stops_capture_pipeline():
    provider = Mem0MemoryProvider()
    pipeline = MagicMock()
    provider._capture_pipeline = pipeline

    provider.shutdown()

    pipeline.stop.assert_called_once_with()


def test_completed_prefetch_retires_idle_executor(monkeypatch):
    provider = Mem0MemoryProvider()
    monkeypatch.setattr(provider, "_prefetch_specificity_gated", lambda _query: True)

    provider.queue_prefetch("ok")
    future = provider._prefetch_future
    assert future is not None
    future.result(timeout=5.0)

    assert _wait_until(lambda: provider._prefetch_executor is None), (
        "completed prefetch retained its executor worker"
    )
    provider.shutdown()


def test_one_capture_drain_owner_per_durable_queue(tmp_path):
    store = _BlockingStore()
    first = _capture_pipeline(tmp_path, store)

    assert first.enqueue_turn("first durable fact", "ok", session_id="s", turn_ordinal=1)
    assert store.started.wait(timeout=5.0)

    second = _capture_pipeline(tmp_path, store)
    assert second.enqueue_turn("second durable fact", "ok", session_id="s", turn_ordinal=2)
    assert first._worker._thread is not None
    assert first._worker._thread.is_alive()
    assert second._worker._thread is None

    store.release.set()
    assert _wait_until(lambda: len(store.rows) == 2)
    first._worker._thread.join(timeout=2.0)
    assert not first._worker._thread.is_alive()
    first.stop()
    second.stop()


def test_idle_drain_worker_exits_and_restarts_for_later_work(tmp_path):
    """A cached provider must not retain one polling thread after its queue drains."""
    store = _FakeStore()
    pipeline = _capture_pipeline(tmp_path, store)

    assert pipeline.enqueue_turn("first durable fact", "ok", session_id="s", turn_ordinal=1)
    assert _wait_until(lambda: len(store.rows) == 1)
    first_thread = pipeline._worker._thread
    assert first_thread is not None
    first_thread.join(timeout=2.0)
    assert not first_thread.is_alive(), "empty capture queue retained an idle poller"

    assert pipeline.enqueue_turn("second durable fact", "ok", session_id="s", turn_ordinal=2)
    assert _wait_until(lambda: len(store.rows) == 2)
    assert pipeline._worker._thread is not first_thread
    pipeline.stop()
