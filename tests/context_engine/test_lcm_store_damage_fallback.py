"""A damaged lcm.db must not stop compaction (t_5be6185c, 2026-11-11 incident).

After a kernel panic lcm.db raised ``database disk image is malformed`` on every
pass. The fail-open path only ran the store-free fallback above the MODEL limit,
so sessions over the compaction threshold got no compaction at all until they
hit the provider wall.
"""
import sqlite3

import pytest

from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine
from plugins.context_engine.lcm.tokens import count_messages_tokens


def _malformed(*_args, **_kwargs):
    raise sqlite3.DatabaseError("database disk image is malformed")


class _MalformedConnection:
    """Stands in for the store's sqlite connection on a corrupted file: every
    statement and transaction raises the error the live lcm.db raised."""

    __enter__ = _malformed

    def __exit__(self, *_exc):
        return False

    def close(self):
        return None

    def __getattr__(self, name):
        return _malformed


class _RecordingFallback:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False):
        self.calls += 1
        return self.result


def _engine(tmp_path, *, context_length=100_000):
    config = LCMConfig(
        database_path=str(tmp_path / "lcm.db"),
        fresh_tail_count=1,
        leaf_chunk_tokens=1,
        context_threshold=0.01,
        fresh_tail_token_budget_enabled=False,
    )
    engine = LCMEngine(config=config, hermes_home=str(tmp_path))
    engine.update_model("unit-test-model", context_length, provider="unit-test")
    engine.on_session_start(
        "session-1",
        hermes_home=str(tmp_path),
        model="unit-test-model",
        provider="unit-test",
        context_length=context_length,
        platform="pytest",
    )
    return engine


def _messages():
    blob = "overflow payload " * 400
    return [
        {"role": "system", "content": "You are testing LCM."},
        {"role": "user", "content": blob},
        {"role": "assistant", "content": blob},
        {"role": "user", "content": blob},
    ]


def _break_store(engine):
    engine._store._conn = _MalformedConnection()


def test_malformed_store_over_threshold_runs_fallback_and_shrinks(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    messages = _messages()
    raw_tokens = count_messages_tokens(messages)
    assert engine.threshold_tokens <= raw_tokens < engine.context_length
    fallback_result = [messages[0], {"role": "assistant", "content": "summary"}, messages[-1]]
    fallback = _RecordingFallback(fallback_result)
    monkeypatch.setattr(engine, "_build_fail_open_fallback_compressor", lambda: fallback)
    _break_store(engine)

    returned = engine.compress(messages, current_tokens=raw_tokens)

    assert fallback.calls == 1
    assert count_messages_tokens(returned) < raw_tokens
    assert len(returned) < len(messages)
    # get_status() itself queries the (broken) store, so read the fail-open fields directly.
    assert engine._last_fail_open_error_type == "DatabaseError"
    assert engine._last_fail_open_fallback_status == "succeeded"
    assert engine.last_compress_store_unavailable is True


def test_malformed_store_below_threshold_keeps_degrade_unchanged(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    fallback = _RecordingFallback([])
    monkeypatch.setattr(engine, "_build_fail_open_fallback_compressor", lambda: fallback)
    _break_store(engine)
    monkeypatch.setattr(engine, "threshold_tokens", 10_000)

    returned = engine.compress(messages, current_tokens=count_messages_tokens(messages))

    assert returned is messages
    assert fallback.calls == 0
    assert engine._last_fail_open_fallback_status == "not_needed_below_limit"


def test_non_store_error_over_threshold_keeps_degrade_unchanged(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    messages = _messages()
    fallback = _RecordingFallback([])
    monkeypatch.setattr(engine, "_build_fail_open_fallback_compressor", lambda: fallback)

    def _raise(*_a, **_k):
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(engine, "_ingest_messages", _raise)

    returned = engine.compress(messages, current_tokens=count_messages_tokens(messages))

    assert returned is messages
    assert fallback.calls == 0
    assert engine.last_compress_store_unavailable is False


def test_malformed_store_fallback_failure_under_model_limit_degrades(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    messages = _messages()

    class _Boom:
        def compress(self, *_a, **_k):
            raise RuntimeError("summariser down")

    monkeypatch.setattr(engine, "_build_fail_open_fallback_compressor", lambda: _Boom())
    _break_store(engine)

    returned = engine.compress(messages, current_tokens=count_messages_tokens(messages))

    assert returned is messages
    assert engine._last_fail_open_fallback_status == "failed"


@pytest.mark.parametrize(
    "message",
    ["database is locked", "database table is locked", "disk I/O error", "unable to open database file",
     "attempt to write a readonly database", "database or disk is full"],
)
def test_transient_store_error_over_threshold_keeps_degrade_unchanged(tmp_path, monkeypatch, message):
    """Prism 824ad63e76fb: only corruption is store damage. A retryable sqlite error must not
    trigger the lossy store-free fallback at the compaction threshold."""
    engine = _engine(tmp_path)
    messages = _messages()
    fallback = _RecordingFallback([messages[0]])
    monkeypatch.setattr(engine, "_build_fail_open_fallback_compressor", lambda: fallback)

    def _raise(*_a, **_k):
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(engine, "_ingest_messages", _raise)

    returned = engine.compress(messages, current_tokens=count_messages_tokens(messages))

    assert returned is messages
    assert fallback.calls == 0
    assert engine.last_compress_store_unavailable is False


def test_store_damage_classifier_uses_sqlite_errorcode():
    locked = sqlite3.OperationalError("weird text, malformed")
    locked.sqlite_errorcode = 5  # SQLITE_BUSY: the code wins over the text
    corrupt = sqlite3.DatabaseError("some text")
    corrupt.sqlite_errorcode = 11  # SQLITE_CORRUPT
    notadb = sqlite3.DatabaseError("x")
    notadb.sqlite_errorcode = 26  # SQLITE_NOTADB
    assert LCMEngine._is_store_damage_error(locked) is False
    assert LCMEngine._is_store_damage_error(corrupt) is True
    assert LCMEngine._is_store_damage_error(notadb) is True
    assert LCMEngine._is_store_damage_error(RuntimeError("database disk image is malformed")) is False


def test_real_corrupt_file_is_classified_as_store_damage(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    with pytest.raises(sqlite3.DatabaseError) as info:
        sqlite3.connect(bad).execute("select * from sqlite_master").fetchall()
    assert LCMEngine._is_store_damage_error(info.value) is True
