"""Migration must converge to the key the live adapter will actually use."""
import json
from datetime import datetime, timedelta

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from hermes_state import SessionDB


@pytest.mark.parametrize("kind", ["dm", "group", "thread"])
@pytest.mark.parametrize("backend", ["json", "sqlite"])
@pytest.mark.parametrize("origin_present", [True, False])
def test_legacy_aliases_converge_to_live_key(tmp_path, kind, backend, origin_present):
    now = datetime.now()
    live = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type=kind,
                         user_id="456", thread_id="123" if kind == "thread" else None)
    canonical = build_session_key(live)
    legacy = "agent:main:discord:channel:123:456"
    pin = {"model": "pinned", "provider": "openrouter"}
    older = SessionEntry(session_key=canonical, session_id="older", created_at=now,
                         updated_at=now, origin=live if origin_present else None, model_override=pin)
    newer = SessionEntry(session_key=legacy, session_id="newer", created_at=now,
                         updated_at=now + timedelta(seconds=1),
                         origin=SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="channel", user_id="456") if origin_present else None)
    data = {canonical: older.to_dict(), legacy: newer.to_dict()}
    (tmp_path / "sessions.json").write_text(json.dumps(data))
    db = SessionDB(tmp_path / "state.db") if backend == "sqlite" else None
    if db:
        # Simulate an old release's pre-guard durable rows, not a new legal write.
        db._execute_write(lambda conn: conn.executemany(
            "INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at) VALUES (?, ?, ?, ?)",
            [(str(tmp_path.resolve()), key, json.dumps(value), now.timestamp()) for key, value in data.items()],
        ))
    store = SessionStore(tmp_path, GatewayConfig())
    store._db = db
    store._ensure_loaded()
    store.migrate_discord_session_keys({"123": kind})
    assert {entry.session_key for entry in store.snapshot_entries()} == {canonical}
    assert store.entry_for(canonical).session_id == "newer"
    assert store.entry_for(canonical).model_override == pin
    assert store.get_or_create_session(live).session_key == canonical
    restored = SessionStore(tmp_path, GatewayConfig())
    restored._db = db
    assert {entry.session_key for entry in restored.snapshot_entries()} == {canonical}
    assert restored.migrate_discord_session_keys({"123": kind}) == 0
    if db:
        db.close()
