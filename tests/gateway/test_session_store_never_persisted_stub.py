"""Regression coverage for never-persisted gateway routing stubs.

A routing entry can be committed before its matching ``sessions`` row.  When
session-row creation then fails, the zero-activity route survives indefinitely
because missing rows are also how genuine pre-SQLite routes appear.  These
tests preserve active legacy routes while proving an old inert stub heals
before the next message is persisted.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionSource, SessionStore


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="8494508720",
        chat_type="dm",
        user_id="8494508720",
    )


def _config() -> GatewayConfig:
    return GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))


def _entry(
    key: str,
    session_id: str,
    *,
    age: timedelta,
    updated_after_create: bool = False,
    had_any_turn: bool = False,
    last_prompt_tokens: int = 0,
) -> SessionEntry:
    created_at = datetime.now() - age
    return SessionEntry(
        session_key=key,
        session_id=session_id,
        created_at=created_at,
        updated_at=(
            created_at + timedelta(minutes=1)
            if updated_after_create
            else created_at
        ),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        origin=_source(),
        had_any_turn=had_any_turn,
        last_prompt_tokens=last_prompt_tokens,
    )


def _seed_routes(store: SessionStore, entries: list[SessionEntry]) -> None:
    store.sessions_dir.mkdir(parents=True, exist_ok=True)
    assert store._db is not None
    payloads = {}
    for entry in entries:
        payload = entry.to_dict()
        payloads[entry.session_key] = payload
        store._db.save_gateway_routing_entry(
            entry.session_key,
            json.dumps(payload),
            scope=store._routing_scope(),
        )
    (store.sessions_dir / "sessions.json").write_text(
        json.dumps(payloads, indent=2),
        encoding="utf-8",
    )


def test_phase0_old_never_persisted_route_heals_before_real_message(tmp_path, monkeypatch):
    """An old inert route must not swallow the next real transcript write."""
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    sessions_dir = home / "sessions"
    source = _source()

    seed = SessionStore(sessions_dir=sessions_dir, config=_config())
    assert seed._db is not None
    key = seed._generate_session_key(source)
    stale_id = "never-persisted-stub"
    _seed_routes(
        seed,
        [_entry(key, stale_id, age=timedelta(days=2))],
    )
    seed._db.close()

    store = SessionStore(sessions_dir=sessions_dir, config=_config())
    assert store._db is not None
    entry = store.get_or_create_session(source)
    store.append_to_transcript(
        entry.session_id,
        {"role": "user", "content": "real message after orphaned create"},
    )

    observed = {
        "returned_never_persisted_id": entry.session_id == stale_id,
        "session_row_exists": store._db.get_session(entry.session_id) is not None,
        "persisted_user_messages": len(
            store._db.get_messages_as_conversation(entry.session_id)
        ),
        "pending_transcript_messages": len(
            store._dirty_transcripts.get(entry.session_id, [])
        ),
    }
    assert observed == {
        "returned_never_persisted_id": False,
        "session_row_exists": True,
        "persisted_user_messages": 1,
        "pending_transcript_messages": 0,
    }
    store._db.close()


def test_startup_reaps_only_old_inert_missing_rows_and_roundtrips_both_stores(
    tmp_path, monkeypatch
):
    """Active legacy and live routes survive the dual-store cleanup cycle."""
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    sessions_dir = home / "sessions"

    seed = SessionStore(sessions_dir=sessions_dir, config=_config())
    assert seed._db is not None
    entries = [
        _entry("old-stub", "sid-old-stub", age=timedelta(days=2)),
        _entry("recent-stub", "sid-recent-stub", age=timedelta(minutes=1)),
        _entry(
            "legacy-with-message",
            "sid-legacy-message",
            age=timedelta(days=30),
            had_any_turn=True,
        ),
        _entry(
            "legacy-with-tokens",
            "sid-legacy-tokens",
            age=timedelta(days=30),
            last_prompt_tokens=1,
        ),
        _entry(
            "legacy-with-update",
            "sid-legacy-update",
            age=timedelta(days=30),
            updated_after_create=True,
        ),
        _entry("live", "sid-live", age=timedelta(days=30)),
    ]
    seed._db.create_session("sid-live", source="telegram")
    _seed_routes(seed, entries)
    seed._db.close()

    startup = SessionStore(sessions_dir=sessions_dir, config=_config())
    assert startup._db is not None
    expected_keys = {
        "recent-stub",
        "legacy-with-message",
        "legacy-with-tokens",
        "legacy-with-update",
        "live",
    }
    assert {entry.session_key for entry in startup.snapshot_entries()} == expected_keys
    assert set(
        startup._db.load_gateway_routing_entries(scope=startup._routing_scope())
    ) == expected_keys
    assert (
        set(json.loads((sessions_dir / "sessions.json").read_text(encoding="utf-8")))
        - {"_README"}
    ) == expected_keys
    startup._db.close()

    # This gateway starts after cleanup, so it never holds old-stub in memory.
    # A subsequent full-index persist must not resurrect the deleted route in
    # either the authoritative SQLite table or the legacy JSON mirror.
    fresh = SessionStore(sessions_dir=sessions_dir, config=_config())
    assert fresh._db is not None
    assert {entry.session_key for entry in fresh.snapshot_entries()} == expected_keys
    fresh.persist()
    assert set(
        fresh._db.load_gateway_routing_entries(scope=fresh._routing_scope())
    ) == expected_keys
    assert (
        set(json.loads((sessions_dir / "sessions.json").read_text(encoding="utf-8")))
        - {"_README"}
    ) == expected_keys
    fresh._db.close()


def test_runtime_reaps_inert_missing_row_after_grace_without_restart(
    tmp_path, monkeypatch
):
    """A failed create that ages in a live gateway heals on the next message."""
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    sessions_dir = home / "sessions"
    source = _source()

    seed = SessionStore(sessions_dir=sessions_dir, config=_config())
    assert seed._db is not None
    key = seed._generate_session_key(source)
    stale_id = "stub-that-ages-in-process"
    route = _entry(key, stale_id, age=timedelta(minutes=1))
    _seed_routes(seed, [route])
    seed._db.close()

    store = SessionStore(sessions_dir=sessions_dir, config=_config())
    assert store._db is not None
    # Load without routing a message: get_or_create_session intentionally bumps
    # updated_at, which is itself activity evidence and must protect the route.
    assert store.peek_session_id(key) == stale_id

    with patch(
        "gateway.session._now",
        return_value=route.created_at + timedelta(days=2),
    ):
        healed = store.get_or_create_session(source)

    assert healed.session_id != stale_id
    assert store._db.get_session(healed.session_id) is not None
    persisted = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert set(persisted) == {key}
    assert json.loads(persisted[key])["session_id"] == healed.session_id
    store._db.close()
