"""kanban_watchers._session_origin reuses one SessionDB inside the notifier (t_69e10010).

It used to construct (and close) a writable SessionDB per actor per subscription per tick; each
construction ran the full open path, fd-table scans included, under the startup lock.
"""

import asyncio
import json

import pytest

import gateway.kanban_watchers as kw
import hermes_state
import hermes_state_registry
from hermes_state import SessionDB


ORIGINS = {
    f"sess-{i}": {"platform": "telegram", "chat_id": f"chat-{i % 7}", "session_id": "",
                  "user_id": f"u{i % 3}", "profile": "default"}
    for i in range(10)
}


@pytest.fixture
def seeded_state_db():
    db = SessionDB()
    try:
        for sid, origin in ORIGINS.items():
            db.create_session(sid, "telegram")
            db.record_gateway_session_peer(
                sid, source="telegram", session_key=f"key-{sid}", chat_id=origin["chat_id"],
                origin_json=json.dumps(origin))
        db.create_session("sess-no-origin", "cli")
    finally:
        db.close()
    # Relative to whatever an earlier test in this worker left in the process-wide registry
    # (other paths); every assertion below is a delta from this baseline.
    yield hermes_state_registry.stats()


def _ids(n=100):
    keys = list(ORIGINS) + ["sess-no-origin", "sess-missing"]
    return [keys[i % len(keys)] for i in range(n)]


def test_warmed_reader_constructs_no_sessiondb_and_matches_per_call(seeded_state_db, monkeypatch):
    expected = [kw._session_origin(sid) for sid in _ids()]  # per-call construction (no reader)
    assert expected[0] == ORIGINS["sess-0"] and None in expected

    reader = kw._SessionOriginReader()
    token = kw._ORIGIN_READER.set(reader)
    try:
        kw._session_origin("sess-0")  # warm-up: the one open
        inits = {"n": 0}
        real_init = SessionDB.__init__

        def counting_init(self, *a, **k):
            inits["n"] += 1
            return real_init(self, *a, **k)

        monkeypatch.setattr(hermes_state.SessionDB, "__init__", counting_init)
        got = [kw._session_origin(sid) for sid in _ids()]
        assert inits["n"] == 0
        assert got == expected
    finally:
        kw._ORIGIN_READER.reset(token)
        reader.close()
    # Every reference the reader took is returned.
    assert hermes_state_registry.stats()["live_generations"] == seeded_state_db["live_generations"]


def test_reader_shares_the_registry_handle_the_gateway_store_holds(seeded_state_db):
    held = hermes_state_registry.acquire()  # stands in for the runner's session store
    reader = kw._SessionOriginReader()
    try:
        token = kw._ORIGIN_READER.set(reader)
        try:
            assert kw._session_origin("sess-3") == ORIGINS["sess-3"]
        finally:
            kw._ORIGIN_READER.reset(token)
        assert list(reader._dbs.values()) == [held]
        assert hermes_state_registry.stats()["total_refcounts"] == seeded_state_db["total_refcounts"] + 2
    finally:
        reader.close()
        hermes_state_registry.release(held)
    assert hermes_state_registry.stats()["live_generations"] == seeded_state_db["live_generations"]


def test_notifier_watcher_sets_reader_for_collect_and_closes_it(seeded_state_db, monkeypatch):
    seen = {}

    async def fake_loop(self, interval=5.0):
        # _collect runs via asyncio.to_thread, which copies this context.
        seen["reader"] = await asyncio.to_thread(kw._ORIGIN_READER.get)
        seen["origin"] = await asyncio.to_thread(kw._session_origin, "sess-1")

    monkeypatch.setattr(kw.GatewayKanbanWatchersMixin, "_kanban_notifier_watcher_loop", fake_loop)
    asyncio.run(kw.GatewayKanbanWatchersMixin()._kanban_notifier_watcher())
    assert isinstance(seen["reader"], kw._SessionOriginReader)
    assert seen["origin"] == ORIGINS["sess-1"]
    assert seen["reader"]._dbs == {}
    assert kw._ORIGIN_READER.get() is None
    assert hermes_state_registry.stats()["live_generations"] == seeded_state_db["live_generations"]
