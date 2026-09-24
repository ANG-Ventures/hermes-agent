"""Per-call ledger: additive migration, exact usage, and parent retention."""
import sqlite3
from types import SimpleNamespace

import pytest

from agent.usage_pricing import CanonicalUsage
from plugins import blackbox
from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord


# Earliest shipped `turns` shape (store.py @ e54eb50327): predates depth,
# cli_invocation_id, the C1 served_subs_json/attribution columns, the
# turn_api_calls table, and every turns index. A live ledger opened by this
# PR's code can be this old, so every AC below also runs migrated-from-here.
_LEGACY_TURNS_COLS = (
    "turn_id TEXT PRIMARY KEY, parent_turn_id TEXT, is_subagent INT,"
    " ts_start REAL, ts_end REAL, profile TEXT, provider TEXT, model TEXT,"
    " platform TEXT, chat_id TEXT, chat_name TEXT, api_calls INT, tools TEXT,"
    " input_tokens INT, output_tokens INT, cache_read INT, cache_write INT,"
    " reasoning INT, context_used INT, context_length INT, cost_usd REAL,"
    " cost_status TEXT, interrupted INT, alerted INT DEFAULT 0,"
    " user_text TEXT, final_text TEXT"
)


@pytest.fixture(params=["fresh", "legacy"])
def db(request, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if request.param == "legacy":
        path = store._db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = sqlite3.connect(str(path))
        legacy.execute(f"CREATE TABLE turns ({_LEGACY_TURNS_COLS})")
        legacy.commit()
        legacy.close()
    conn = store._connect()
    conn.close()
    return store._db_path()


def append(turn_id, seq, **overrides):
    args = dict(ts=100.5, provider="claude-apr", model="test-model",
                usage=CanonicalUsage(1000, 50, 300, 20, 7),
                sub_key="sub-vps-7", attribution="wire")
    args.update(overrides)
    store.insert_api_call(turn_id, seq, **args)


def test_plugin_end_hook_uses_call_ledger_turn_id(db, monkeypatch):
    """The real plugin entry points must persist a joinable call and turn."""
    monkeypatch.setattr(blackbox, '_config', lambda: {
        'enabled': True, 'alerts_enabled': False, 'record_subagents': True,
        'retention_days': 3650,
    })
    turn_id = 'session:task:12345678'
    blackbox._on_session_start(session_id='session')
    blackbox.record_api_call(
        turn_id=turn_id, seq=0, ts=100., provider='claude-apr', model='test-model',
        usage=CanonicalUsage(input_tokens=1000, output_tokens=50),
        api_mode='anthropic_messages', sub_key='sub-vps-7', attribution='wire',
        http_status=200, relay_synthetic=False, route_id=None,
    )
    blackbox._on_session_end(session_id='session', turn_id=turn_id,
                             provider='claude-apr', model='test-model', platform='cli',
                             turn_usage={'input_tokens': 1000, 'output_tokens': 50,
                                         'api_calls': 1})
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM turn_api_calls a JOIN turns t USING (turn_id)').fetchone()[0] == 1
        assert conn.execute('SELECT turn_id FROM turns').fetchone()[0] == turn_id


def test_api_call_exact_roundtrip_and_defaults(db):
    append("t", 0)
    append("t", 1, usage=CanonicalUsage(), sub_key=None,
           http_status=503, relay_synthetic=True, route_id="route-1")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT turn_id,seq,ts,provider,sub_key,model,input_tokens,"
                            "output_tokens,cache_read,cache_write,reasoning,attribution,"
                            "http_status,relay_synthetic,route_id FROM turn_api_calls "
                            "ORDER BY seq").fetchall() == [
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


def test_null_key_parts_rejected(db):
    """NULL PK parts never collide in SQLite, so they must be refused outright."""
    with pytest.raises(ValueError):
        append(None, 0)
    with pytest.raises(ValueError):
        append("t", None)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turn_api_calls").fetchone()[0] == 0
        # The DDL enforces it too, independent of the Python guard.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO turn_api_calls (turn_id, seq) VALUES (NULL, 0)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO turn_api_calls (turn_id, seq) VALUES ('t', NULL)")


def test_duplicate_key_raises_integrity_error(db):
    append("t", 0)
    with pytest.raises(sqlite3.IntegrityError):
        append("t", 0)


def test_sweep_deletes_old_orphans_but_keeps_young_ones(db, monkeypatch):
    """A call whose parent turn never landed must still age out of retention."""
    monkeypatch.setattr(store.time, "time", lambda: 200000)
    cutoff = 200000 - 86400
    append("orphan-old", 0, ts=1.0)
    append("orphan-young", 0, ts=cutoff + 10)
    store.insert_turn(TurnRecord(turn_id="parented-young", ts_end=200000))
    append("parented-young", 0, ts=1.0)
    store.sweep(retention_days=1)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT turn_id FROM turn_api_calls ORDER BY turn_id"
        ).fetchall() == [("orphan-young",), ("parented-young",)]


def test_orphan_sweep_respects_grace_period_independent_of_retention(db, monkeypatch):
    """A parentless row is the normal state of an IN-FLIGHT turn.

    Retention alone is not a safe predicate: with a short retention window a
    turn still running would have its call ledger deleted before finalize.
    Deletion requires the row to be past retention AND past the grace period.
    """
    now = 10_000_000.0
    monkeypatch.setattr(store.time, "time", lambda: now)
    # retention_days=0 -> retention cutoff is "now"; both rows are past it.
    append("in-flight", 0, ts=now - 2)
    append("long-dead", 0, ts=now - store._ORPHAN_GRACE_S - 1)
    store.sweep(retention_days=0)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT turn_id FROM turn_api_calls"
        ).fetchall() == [("in-flight",)]


def test_reinsert_turn_preserves_rollup_columns(db):
    """insert_turn must refresh its own columns without erasing the C2 rollup."""
    store.insert_turn(TurnRecord(turn_id="t", model="first"))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE turns SET served_subs_json = ?, attribution = ? WHERE turn_id = ?",
            ('{"sub-vps-7": 3}', "wire", "t"),
        )
    store.insert_turn(TurnRecord(turn_id="t", model="second"))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT model, served_subs_json, attribution FROM turns WHERE turn_id = ?",
            ("t",),
        ).fetchall() == [("second", '{"sub-vps-7": 3}', "wire")]


def test_plugin_boundary_normalizes_success_and_zero_token_failure(db, monkeypatch):
    monkeypatch.setattr(blackbox, "_config", lambda: {"enabled": True})
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=50,
        cache_read_input_tokens=300,
        cache_creation_input_tokens=20,
    )
    blackbox.record_api_call(
        turn_id="t",
        seq=0,
        ts=100.0,
        provider="claude-apr",
        model="test-model",
        usage=None,
        api_mode="anthropic_messages",
        sub_key="sub-vps-2",
        attribution="wire",
        http_status=429,
        relay_synthetic=True,
        route_id="route-0",
    )
    blackbox.record_api_call(
        turn_id="t",
        seq=1,
        ts=101.0,
        provider="claude-apr",
        model="test-model",
        usage=usage,
        api_mode="anthropic_messages",
        sub_key="sub-vps-7",
        attribution="wire",
        http_status=200,
        relay_synthetic=False,
        route_id="route-1",
    )
    store.insert_turn(
        TurnRecord(
            turn_id="t",
            input_tokens=1000,
            output_tokens=50,
            cache_read_tokens=300,
            cache_write_tokens=20,
        )
    )

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            """
            SELECT seq, sub_key, input_tokens, output_tokens, cache_read,
                   cache_write, reasoning, http_status, relay_synthetic
            FROM turn_api_calls ORDER BY seq
            """
        ).fetchall()
        assert rows == [
            (0, "sub-vps-2", 0, 0, 0, 0, 0, 429, 1),
            (1, "sub-vps-7", 1000, 50, 300, 20, 0, 200, 0),
        ]
        mismatch_count = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT t.turn_id
                FROM turns t JOIN turn_api_calls c ON c.turn_id = t.turn_id
                GROUP BY t.turn_id
                HAVING SUM(c.input_tokens) != t.input_tokens
                    OR SUM(c.output_tokens) != t.output_tokens
                    OR SUM(c.cache_read) != t.cache_read
                    OR SUM(c.cache_write) != t.cache_write
                    OR SUM(c.reasoning) != t.reasoning
            )
            """
        ).fetchone()[0]
        assert mismatch_count == 0
