"""C5 races in the gateway session store and its off-loop helpers.

Each test forces the interleaving the FleetReview finding describes and
asserts the store ends consistent (red on the pre-fix code).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from gateway import telegram_redelivery as tgr
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner


def _store(tmp_path) -> SessionStore:
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None  # deterministic JSON fallback
    return store


def _key(store, chat_id="c5-chat"):
    source = SessionSource(platform=Platform.DISCORD, chat_id=chat_id,
                           user_id="u1", chat_type="channel", thread_id="t1")
    store.get_or_create_session(source)
    return store._generate_session_key(source)


def test_out_of_order_turn_marker_publish_keeps_newest(tmp_path, monkeypatch):
    """PR #1043 'Stale publication'/'Stale marker': A persisted before B but
    published after it must not replace B's live token."""
    store = _store(tmp_path)
    key = _key(store)
    real = store._publish_persisted_entry
    deferred = []

    def hook(*args):
        if not deferred:
            deferred.append(args)
            return
        real(*args)

    monkeypatch.setattr(store, "_publish_persisted_entry", hook)
    store.mark_turn_active(key)
    tok_b = store.mark_turn_active(key)
    real(*deferred[0])  # A's publish lands last
    assert store._entries[key].active_turn_token == tok_b


def test_clear_override_does_not_erase_a_newer_set(tmp_path, monkeypatch):
    """PR #1043 'Override race': a /model set between the clear's capture and
    its publish survives."""
    store = _store(tmp_path)
    key = _key(store, "c5-override")
    store.set_model_override(key, {"model": "m1", "provider": "p1"})
    real = store._publish_persisted_entry

    def hook(*args):
        store.set_model_override(key, {"model": "m2", "provider": "p2"})
        real(*args)

    monkeypatch.setattr(store, "_publish_persisted_entry", hook)
    store.clear_model_route_override(key)
    assert (store._entries[key].model_override or {}).get("model") == "m2"


def test_prune_skips_route_healed_in_place(tmp_path):
    """PR #1043 'Stale prune': the entry object is rewritten in place (heal)
    while the plan's DB lookup runs; the healed route must not be pruned."""
    store = _store(tmp_path)
    key = _key(store, "c5-prune")
    entry = store._entries[key]
    entry.origin = None

    class _Db:
        def get_session(self, sid):
            entry.session_id = "healed-" + sid  # compression-tip heal, same object
            return {"end_reason": "compressed"}

    store._db = _Db()
    store._prune_stale_sessions_off_lock()
    store._db = None
    assert key in store._entries


def test_hwm_advance_during_flush_is_not_lost(tmp_path, monkeypatch):
    """PR #1043 'Serialize Telegram redelivery HWM': an observe racing a
    checkpoint must leave the tracker dirty (or block until the write ends)."""
    tracker = tgr.TelegramHwmTracker(tmp_path, "default", checkpoint_interval_secs=0)
    tracker.observe_dispatch(100)
    real_replace = tgr.os.replace
    racer = {}

    def slow_replace(src, dst):
        t = threading.Thread(target=tracker.observe_dispatch, args=(200,))
        t.start()
        t.join(timeout=0.2)
        racer["thread"] = t
        real_replace(src, dst)

    monkeypatch.setattr(tgr.os, "replace", slow_replace)
    assert tracker.maybe_checkpoint(force=True)
    racer["thread"].join(timeout=5)
    assert tracker.value == 200
    assert tracker._dirty is True


def test_stuck_loop_suspend_holds_store_lock(tmp_path, monkeypatch):
    """PR #1043: the off-loop suspension mutates entries and snapshots under
    the session-store lock."""
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner, _ = make_restart_runner()
    for _ in range(3):
        runner._increment_restart_failure_counts({"session:a"})

    class _Lock:
        depth = 0

        def __enter__(self):
            _Lock.depth += 1

        def __exit__(self, *exc):
            _Lock.depth -= 1

    entry = MagicMock()
    entry.suspended = False
    seen = {}
    runner.session_store._lock = _Lock()
    runner.session_store._entries = {"session:a": entry}
    runner.session_store._save = lambda: seen.setdefault("depth", _Lock.depth)
    assert runner._suspend_stuck_loop_sessions() == 1
    assert seen["depth"] == 1
