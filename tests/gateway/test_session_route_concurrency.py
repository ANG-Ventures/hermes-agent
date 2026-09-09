"""Independent routing writers cannot create type aliases, even with stale caches."""
import json
import multiprocessing
from dataclasses import replace
from datetime import datetime

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore
from hermes_state import SessionDB


def _entry(kind):
    now = datetime.now()
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type=kind, user_id="456")
    key = f"agent:main:discord:{kind}:123:456"
    return SessionEntry(session_key=key, session_id=kind, created_at=now, updated_at=now, origin=source)


def _race_writer(path, kind, writer, ready, start, results):
    db = SessionDB(path)
    entry = _entry(kind)
    ready.put(kind)
    start.wait(20)
    try:
        if writer == "upsert":
            db.save_gateway_routing_entry(entry.session_key, json.dumps(entry.to_dict()), scope="test")
        else:
            db.replace_gateway_routing_entries({entry.session_key: json.dumps(entry.to_dict())}, scope="test")
        results.put("saved")
    except ValueError:
        results.put("refused")
    finally:
        db.close()


@pytest.mark.parametrize("writer", ["upsert", "snapshot"])
def test_two_process_writers_refuse_second_type(tmp_path, writer):
    path = tmp_path / "state.db"
    SessionDB(path).close()
    ctx = multiprocessing.get_context("spawn")
    ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_race_writer, args=(path, kind, writer, ready, start, results)) for kind in ("group", "channel")]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            ready.get(timeout=30)
        start.set()
        outcomes = [results.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(30)
            assert worker.exitcode == 0
        assert sorted(outcomes) == ["refused", "saved"]
        db = SessionDB(path)
        assert len(db.load_gateway_routing_entries(scope="test")) == 1
        db.close()
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()


@pytest.mark.parametrize("backend", ["sqlite", "json"])
@pytest.mark.parametrize("writer", ["upsert", "snapshot"])
def test_stale_store_cannot_bypass_guard_or_fallback(tmp_path, backend, writer):
    db = SessionDB(tmp_path / "state.db") if backend == "sqlite" else None
    first = SessionStore(tmp_path, GatewayConfig())
    second = SessionStore(tmp_path, GatewayConfig())
    first._db = second._db = db
    first._ensure_loaded()
    second._ensure_loaded()
    entry = first.get_or_create_session(_entry("group").origin)
    before = (tmp_path / "sessions.json").read_bytes()
    alias = replace(entry, session_key=entry.session_key.replace(":group:", ":channel:"), session_id="second")
    second._entries[alias.session_key] = alias
    alerts = []
    second.on_session_key_conflict = alerts.append
    with pytest.raises(ValueError, match="session.*route|session-key collision"):
        second._save_entry(alias.session_key) if writer == "upsert" else second.persist()
    assert alias.session_key not in second._entries
    assert len(alerts) == 1
    assert (tmp_path / "sessions.json").read_bytes() == before
    if db:
        assert set(db.load_gateway_routing_entries(scope=first._routing_scope())) == {entry.session_key}
        db.close()
