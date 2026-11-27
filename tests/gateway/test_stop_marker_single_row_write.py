"""The /stop marker is a one-row write, durable before mark_user_stopped returns.

Live 2026-11-08 (PHASE=user_stopped store_s): 0.6 s, 5.5 s and 2.8 s on three real stops. The
marker went through the store's full routing rewrite (every entry to state.db plus the multi-MB
sessions.json mirror), so /stop queued behind every other session's save. Only one entry's marker
fields change, so it now uses the existing single-entry UPSERT. These tests drive a real
SessionStore over a real state.db.
"""

import json

from tests.gateway.test_user_stopped_never_resumes import _runner, _source


def _full_rewrites(store, calls):
    real = store._persist_routing_data

    def spy(*args, **kwargs):
        calls.append("full")
        return real(*args, **kwargs)

    store._persist_routing_data = spy


def _durable_entry(store, key):
    loader = store._routing_db_method("load_gateway_routing_entries")
    rows = loader(scope=store._routing_scope())
    raw = rows[key]
    return json.loads(raw) if isinstance(raw, str) else raw


def test_mark_and_clear_skip_the_full_rewrite_and_land_on_disk(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    key = store.get_or_create_session(_source()).session_key
    calls = []
    _full_rewrites(store, calls)

    assert store.mark_user_stopped(key, generation=3, last_message_id=42) is True
    assert calls == [], "the stop marker must not rewrite the whole routing index"
    durable = _durable_entry(store, key)
    assert durable["user_stopped_at"] and durable["user_stopped_message_id"] == 42

    assert store.clear_user_stopped(key, generation=3) is True
    assert calls == [], "clearing the marker must not rewrite the whole routing index"
    assert _durable_entry(store, key)["user_stopped_at"] is None
    db.close()


def test_out_of_order_stop_still_persists_the_newer_marker_with_one_row(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    key = store.get_or_create_session(_source()).session_key
    assert store.mark_user_stopped(key, generation=8, last_message_id=7) is True
    calls = []
    _full_rewrites(store, calls)

    assert store.mark_user_stopped(key, generation=6, last_message_id=5) is True  # late, older stop
    assert calls == []
    assert _durable_entry(store, key)["user_stopped_message_id"] == 7  # newer marker kept on disk
    db.close()
