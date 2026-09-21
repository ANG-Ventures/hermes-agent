"""Per-call ledger: additive migration, exact usage, and parent retention."""
import sqlite3

import pytest

from agent.usage_pricing import CanonicalUsage
from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = store._connect()
    conn.close()
    return store._db_path()


def append(turn_id, seq, **overrides):
    args = dict(ts=100.5, provider="claude-apr", model="test-model",
                usage=CanonicalUsage(1000, 50, 300, 20, 7),
                sub_key="sub-vps-7", attribution="wire")
    args.update(overrides)
    store.insert_api_call(turn_id, seq, **args)


def test_api_call_exact_roundtrip_and_defaults(db):
    append("t", 0)
    append("t", 1, usage=CanonicalUsage(), sub_key=None,
           http_status=503, relay_synthetic=True, route_id="route-1")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM turn_api_calls ORDER BY seq").fetchall() == [
            ("t", 0, 100.5, "claude-apr", "sub-vps-7", "test-model",
             1000, 50, 300, 20, 7, "wire", None, 0, None),
            ("t", 1, 100.5, "claude-apr", None, "test-model",
             0, 0, 0, 0, 0, "wire", 503, 1, "route-1"),
        ]
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0


@pytest.mark.parametrize("attribution", ["wire", "pinned", "inferred", "external"])
def test_valid_attribution(db, attribution):
    append("t", 0, attribution=attribution)


def test_invalid_attribution_and_duplicate_fail_loud(db):
    with pytest.raises(ValueError):
        append("t", 0, attribution="guessed")
    append("t", 0)
    with pytest.raises(sqlite3.IntegrityError):
        append("t", 0)


def test_migration_twice_is_noop(db):
    append("t", 0)
    with sqlite3.connect(db) as conn:
        before = list(conn.iterdump())
        store._ensure_schema(conn)
        store._ensure_schema(conn)
        assert list(conn.iterdump()) == before


def test_sweep_cascades_only_selected_parents(db, monkeypatch):
    monkeypatch.setattr(store.time, "time", lambda: 200000)
    for turn_id, end in [("old", 1), ("keep", 200000)]:
        store.insert_turn(TurnRecord(turn_id=turn_id, ts_end=end))
        append(turn_id, 0)
        append(turn_id, 1)
    assert store.sweep(retention_days=1) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT turn_id FROM turns").fetchall() == [("keep",)]
        assert conn.execute("SELECT turn_id, seq FROM turn_api_calls ORDER BY seq").fetchall() == [
            ("keep", 0), ("keep", 1)]
