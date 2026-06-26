"""Forward-fix tests: LCM ingest preserves real per-message timestamps.

Bug: store.append/append_batch stamped every replayed row with time.time()
at ingest, collapsing whole restart-replay batches onto one instant. Fix:
carry the durable `timestamp` from the source dict through ingest.
"""
import time
import pytest

from plugins.context_engine.lcm.store import _coerce_ts


class TestCoerceTs:
    def test_real_timestamp_preserved(self):
        assert _coerce_ts(1781927362.5) == 1781927362.5

    def test_int_timestamp_preserved(self):
        assert _coerce_ts(1781927362) == 1781927362.0

    def test_numeric_string_preserved(self):
        assert _coerce_ts("1781927362.5") == 1781927362.5

    def test_none_falls_back_to_now(self):
        before = time.time()
        out = _coerce_ts(None)
        assert before <= out <= time.time() + 1

    def test_missing_via_dict_get_falls_back(self):
        before = time.time()
        out = _coerce_ts({}.get("timestamp"))
        assert before <= out <= time.time() + 1

    def test_garbage_string_falls_back(self):
        before = time.time()
        out = _coerce_ts("not-a-timestamp")
        assert before <= out <= time.time() + 1

    def test_zero_treated_as_missing(self):
        # a real message is never legitimately stamped 0; avoid writing 1970
        before = time.time()
        out = _coerce_ts(0)
        assert before <= out <= time.time() + 1

    def test_negative_treated_as_missing(self):
        before = time.time()
        out = _coerce_ts(-5)
        assert before <= out <= time.time() + 1

    def test_nan_falls_back(self):
        before = time.time()
        out = _coerce_ts(float("nan"))
        assert before <= out <= time.time() + 1

    def test_inf_falls_back(self):
        before = time.time()
        out = _coerce_ts(float("inf"))
        assert before <= out <= time.time() + 1

    def test_list_falls_back(self):
        before = time.time()
        out = _coerce_ts([1, 2, 3])
        assert before <= out <= time.time() + 1


class TestStoreReplayPreservesTimestamps:
    """Integration: a restart-replay batch must keep each row's real time."""

    def _store(self, tmp_path):
        from plugins.context_engine.lcm.store import MessageStore
        return MessageStore(str(tmp_path / "lcm.db"))

    def test_append_batch_keeps_per_row_times(self, tmp_path):
        store = self._store(tmp_path)
        base = 1781900000.0
        replay = [
            {"role": "user", "content": "first", "timestamp": base + 0},
            {"role": "assistant", "content": "r1", "timestamp": base + 5},
            {"role": "user", "content": "hours later", "timestamp": base + 7200},
            {"role": "assistant", "content": "r2", "timestamp": base + 7203},
            {"role": "user", "content": "next day", "timestamp": base + 90000},
        ]
        store.append_batch("sess", replay)
        import sqlite3
        ts = [r[0] for r in sqlite3.connect(str(tmp_path / "lcm.db"))
              .execute("SELECT timestamp FROM messages ORDER BY store_id")]
        assert ts == [base + 0, base + 5, base + 7200, base + 7203, base + 90000]
        assert len(set(ts)) == 5  # the bug collapsed these to 1

    def test_append_single_new_message_gets_now(self, tmp_path):
        store = self._store(tmp_path)
        before = time.time()
        store.append("sess", {"role": "user", "content": "brand new"})
        import sqlite3
        ts = sqlite3.connect(str(tmp_path / "lcm.db")).execute(
            "SELECT timestamp FROM messages").fetchone()[0]
        assert before <= ts <= time.time() + 1

    def test_append_batch_mixed_replay_and_new(self, tmp_path):
        store = self._store(tmp_path)
        base = 1781900000.0
        before = time.time()
        batch = [
            {"role": "user", "content": "replayed", "timestamp": base},
            {"role": "assistant", "content": "new, no ts"},  # genuinely new
        ]
        store.append_batch("sess", batch)
        import sqlite3
        ts = [r[0] for r in sqlite3.connect(str(tmp_path / "lcm.db"))
              .execute("SELECT timestamp FROM messages ORDER BY store_id")]
        assert ts[0] == base
        assert before <= ts[1] <= time.time() + 1

