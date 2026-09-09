"""Write-path invariant: one routing key per destination and participant."""
from dataclasses import replace
import json

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def store_and_entry(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="456")
    return store, store.get_or_create_session(source)


@pytest.mark.parametrize("writer", ["snapshot", "upsert"])
@pytest.mark.parametrize("keep_origin", [True, False])
def test_duplicate_route_write_fails_closed_and_reports_once(tmp_path, writer, caplog, keep_origin):
    store, entry = store_and_entry(tmp_path)
    canonical = entry.session_key
    alias = canonical.replace(":group:", ":channel:")
    alerts = []
    store.on_session_key_conflict = alerts.append
    before = (tmp_path / "sessions.json").read_bytes()
    for _ in range(2):
        store._entries[alias] = replace(entry, session_key=alias, session_id="illegal-second-session", origin=entry.origin if keep_origin else None)
        with pytest.raises(ValueError, match="session-key collision"):
            if writer == "snapshot":
                store.persist()
            else:
                store._save_entry(alias)
    assert (tmp_path / "sessions.json").read_bytes() == before
    assert len(alerts) == 1
    assert alias not in store._entries
    assert sum("session-key collision" in rec.message for rec in caplog.records) == 1


def test_distinct_participants_and_chats_remain_independent(tmp_path):
    store, entry = store_and_entry(tmp_path)
    store.get_or_create_session(replace(entry.origin, user_id="789"))
    store.get_or_create_session(replace(entry.origin, chat_id="999"))
    store.persist()
    data = json.loads((tmp_path / "sessions.json").read_text())
    assert len([key for key in data if not key.startswith("_")]) == 3
