"""A shape-only legacy alias is redirected at load, before any replay can write.

Live incident 2026-09-09 16:59 (#Home): the pre-#659 ``channel`` alias for a
guild channel was still loaded when the startup-restore watchdog (30s)
replayed the user's queued message — the adapter-driven migration that would
have retired it only ran at +76s. The write guard correctly refused the fork,
but the refusal surfaced as a raw ``SessionKeyConflict`` error bubble in the
user's own chat. The redirect below is a load-time WARNING, not a relaxation:
two genuinely different owner identities are still refused (see
test_session_route_invariant.py / test_session_key_producer_contract.py).
"""
import json
from datetime import datetime, timedelta

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore
from hermes_state import SessionDB

CHAT, USER = "1546879481904369785", "117431298246705156"
GROUP = f"agent:main:discord:group:{CHAT}:{USER}"
CHANNEL = GROUP.replace(":group:", ":channel:")


def _source(chat_type="group"):
    return SessionSource(platform=Platform.DISCORD, chat_id=CHAT, chat_type=chat_type, user_id=USER)


def _seed(tmp_path, entries, backend):
    (tmp_path / "sessions.json").write_text(json.dumps(entries))
    db = SessionDB(tmp_path / "state.db") if backend == "sqlite" else None
    if db:
        # Pre-guard durable rows from an older release, not a new legal write.
        now = datetime.now().timestamp()
        db._execute_write(lambda conn: conn.executemany(
            "INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at) VALUES (?, ?, ?, ?)",
            [(str(tmp_path.resolve()), key, json.dumps(value), now) for key, value in entries.items()],
        ))
    store = SessionStore(tmp_path, GatewayConfig())
    store._db = db
    return store, db


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_startup_replay_lands_in_canonical_session_despite_loaded_alias(tmp_path, caplog, backend):
    now = datetime.now()
    live = SessionEntry(session_key=GROUP, session_id="live", created_at=now, updated_at=now + timedelta(seconds=5), origin=_source())
    stale = SessionEntry(session_key=CHANNEL, session_id="stale", created_at=now, updated_at=now,
                         origin=_source("channel"))
    store, db = _seed(tmp_path, {GROUP: live.to_dict(), CHANNEL: stale.to_dict()}, backend)
    alerts = []
    store.on_session_key_conflict = alerts.append
    with caplog.at_level("WARNING"):
        # No migrate_discord_session_keys() — this is the replay-before-migration window.
        entry = store.get_or_create_session(_source())
    assert entry.session_key == GROUP
    assert entry.session_id == "live"
    assert alerts == []
    assert not any("session-key collision" in r.message for r in caplog.records)
    redirects = [r for r in caplog.records if "Redirecting legacy session route" in r.message]
    assert len(redirects) == 1 and redirects[0].levelname == "WARNING"
    assert f"{CHANNEL} -> {GROUP}" in redirects[0].message
    assert {e.session_key for e in store.snapshot_entries()} == {GROUP}
    # The redirect is durable: a fresh load sees one canonical route and no
    # residual work for the adapter-driven migration.
    restored = SessionStore(tmp_path, GatewayConfig())
    restored._db = db
    assert {e.session_key for e in restored.snapshot_entries()} == {GROUP}
    assert restored.migrate_discord_session_keys({CHAT: "group"}) == 0
    if db:
        assert set(db.load_gateway_routing_entries(scope=store._routing_scope())) == {GROUP}
        db.close()


def test_lone_alias_is_rekeyed_and_keeps_its_transcript(tmp_path, caplog):
    now = datetime.now()
    stale = SessionEntry(session_key=CHANNEL, session_id="only", created_at=now, updated_at=now,
                         origin=_source("channel"), model_override={"model": "pinned", "provider": "openrouter"})
    store, _ = _seed(tmp_path, {CHANNEL: stale.to_dict()}, "json")
    with caplog.at_level("WARNING"):
        entry = store.get_or_create_session(_source())
    assert entry.session_key == GROUP and entry.session_id == "only"
    assert entry.origin.chat_type == "group"
    assert entry.model_override == {"model": "pinned", "provider": "openrouter"}
    assert store.entry_for(CHANNEL) is None
    assert any("rekeyed session only" in r.message for r in caplog.records)


def test_newest_alias_transcript_wins_and_older_pin_survives(tmp_path):
    now = datetime.now()
    older = SessionEntry(session_key=GROUP, session_id="older", created_at=now, updated_at=now,
                         origin=_source(), model_override={"model": "pinned", "provider": "openrouter"})
    newer = SessionEntry(session_key=CHANNEL, session_id="newer", created_at=now,
                         updated_at=now + timedelta(seconds=1), origin=_source("channel"))
    store, _ = _seed(tmp_path, {GROUP: older.to_dict(), CHANNEL: newer.to_dict()}, "json")
    entry = store.get_or_create_session(_source())
    assert entry.session_id == "newer"
    assert entry.model_override == {"model": "pinned", "provider": "openrouter"}


def test_dropbox_resume_on_alias_key_marks_canonical_session(tmp_path, caplog):
    """An older safe-restart/safe-reboot watcher may still submit the alias spelling."""
    now = datetime.now()
    live = SessionEntry(session_key=GROUP, session_id="live", created_at=now, updated_at=now, origin=_source())
    store, _ = _seed(tmp_path, {GROUP: live.to_dict()}, "json")
    with caplog.at_level("WARNING"):
        assert store.mark_resume_pending(CHANNEL, "restart_interrupted", resume_kind="self", resume_handoff="note")
    entry = store.entry_for(GROUP)
    assert entry.resume_pending and entry.resume_kind == "self" and entry.resume_handoff == "note"
    assert store.entry_for(CHANNEL) is None
    assert any("Redirecting resume mark" in r.message for r in caplog.records)
    # Unknown chats are still unknown — the redirect only maps a retired spelling.
    assert not store.mark_resume_pending(CHANNEL.replace(CHAT, "999"), "restart_interrupted")


def test_genuinely_different_owner_spellings_are_still_refused(tmp_path, caplog):
    """dm vs group is not a shape-only alias: it stays with the adapter migration + guard."""
    now = datetime.now()
    group = SessionEntry(session_key=GROUP, session_id="g", created_at=now, updated_at=now, origin=_source())
    dm_key = f"agent:main:discord:dm:{CHAT}"
    dm = SessionEntry(session_key=dm_key, session_id="d", created_at=now, updated_at=now,
                      origin=SessionSource(platform=Platform.DISCORD, chat_id=CHAT, chat_type="dm", user_id=USER))
    store, _ = _seed(tmp_path, {GROUP: group.to_dict(), dm_key: dm.to_dict()}, "json")
    alerts = []
    store.on_session_key_conflict = alerts.append
    with caplog.at_level("WARNING"):
        with pytest.raises(ValueError, match="session-key collision"):
            store.get_or_create_session(_source())
    assert not any("Redirecting legacy session route" in r.message for r in caplog.records)
    assert any("Legacy duplicate session routes await canonical migration" in r.message for r in caplog.records)
    assert len(alerts) == 1


@pytest.mark.parametrize("kind", ["dm", "thread"])
def test_alias_whose_chat_is_really_dm_or_thread_waits_for_adapter(tmp_path, caplog, kind):
    """A ``channel`` key beside a live dm/thread key is NOT a shape-only defect —
    the redirect must not guess; the adapter-driven migration owns it."""
    now = datetime.now()
    live_source = SessionSource(platform=Platform.DISCORD, chat_id=CHAT, chat_type=kind, user_id=USER,
                                thread_id=CHAT if kind == "thread" else None)
    from gateway.session import build_session_key
    live_key = build_session_key(live_source)
    live = SessionEntry(session_key=live_key, session_id="live", created_at=now, updated_at=now, origin=live_source)
    stale = SessionEntry(session_key=CHANNEL, session_id="stale", created_at=now,
                         updated_at=now + timedelta(seconds=1), origin=_source("channel"))
    store, _ = _seed(tmp_path, {live_key: live.to_dict(), CHANNEL: stale.to_dict()}, "json")
    with caplog.at_level("WARNING"):
        store._ensure_loaded()
    assert {e.session_key for e in store.snapshot_entries()} == {live_key, CHANNEL}
    assert any("not redirected" in r.message and "awaiting adapter migration" in r.message for r in caplog.records)
    assert not any("Legacy session alias redirect failed" in r.message for r in caplog.records)
    assert store.migrate_discord_session_keys({CHAT: kind}) == 1
    assert {e.session_key for e in store.snapshot_entries()} == {live_key}
    assert store.entry_for(live_key).session_id == "stale"
