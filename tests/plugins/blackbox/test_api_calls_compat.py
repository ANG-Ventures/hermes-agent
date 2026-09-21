"""AC7: populated pre-C1 database, VACUUM INTO, then additive migration.

External SQL is frozen from the actual fleet consumers (2026-09-21), so
CI does not need private fleet scripts or access to a real telemetry store.
The unchanged /cost and /context readers/renderers are exercised directly:
SELECT * gains columns, so compare their displayed output, not raw row width.
"""
import json
from pathlib import Path
import sqlite3

import pytest

from plugins.blackbox import commands, last_turn, store
from plugins.blackbox.record import TurnRecord
from agent.usage_pricing import CanonicalUsage

FIXTURES = Path(__file__).parent / "fixtures"
# ~/.hermes/scripts/daily-journal-extract.py:113
JOURNAL = """SELECT ts_end, model, platform, chat_id, chat_name, tools, cost_usd,
                  user_text, final_text, is_subagent
           FROM turns WHERE ts_end>=? AND ts_end<? ORDER BY ts_end ASC"""
# ~/.hermes/scripts/test_tokens_reprice_sweep.py:72-73,88 (rollback probes).
REPRICE_PROBES = (
    "SELECT cost_usd FROM turns WHERE turn_id='tA'",
    "SELECT cost_usd FROM turns WHERE turn_id='tB'",
    "SELECT cost_usd FROM turns WHERE turn_id='tX'",
)
# plugins/blackbox/store.py:backfill_unpriced, used by tokens-reprice-sweep.py.
REPRICE = (
    "SELECT turn_id, model, provider, "
    "COALESCE(input_tokens,0) AS i, COALESCE(output_tokens,0) AS o, "
    "COALESCE(cache_read,0) AS cr, COALESCE(cache_write,0) AS cw "
    "FROM turns WHERE cost_usd IS NULL "
    "AND cost_uncached_usd IS NULL AND cost_cache_read_usd IS NULL "
    "AND cost_cache_write_usd IS NULL AND cost_output_usd IS NULL"
)


@pytest.fixture
def legacy_copy(tmp_path, monkeypatch):
    original = tmp_path / "original.db"
    target = tmp_path / "copy.db"
    with sqlite3.connect(original) as conn:
        conn.executescript((FIXTURES / "pre_api_calls.sql").read_text())
        for i in range(500):
            turn_id = ("tA", "tB", "tX")[i] if i < 3 else f"turn-{i}"
            conn.execute(
                """INSERT INTO turns (
                    turn_id, ts_start, ts_end, profile, provider, model, platform,
                    chat_id, chat_name, is_subagent, tools, api_calls, input_tokens,
                    output_tokens, cache_read, cache_write, reasoning, cost_usd,
                    cost_status, user_text, final_text, context_used, context_length
                ) VALUES (?, ?, ?, 'fixture', 'claude-apr', 'test-model', 'telegram',
                          'chat-1', 'Fixture', ?, '[]', 1, ?, 50, 300, 20, 7, ?,
                          ?, 'hello', 'world', 1320, 200000)""",
                (turn_id, 1000+i, 1001+i, i % 2, 1000+i,
                 None if i % 3 == 0 else i/100,
                 "unknown" if i % 3 == 0 else "estimated"),
            )
        conn.execute("INSERT INTO last_turn VALUES ('telegram', 'chat-1', 'turn-499')")
        conn.commit()
        conn.execute("VACUUM INTO ?", (str(target),))
    monkeypatch.setattr(store, "_db_path", lambda: target)

    # Use the reader unchanged, but don't let its connector migrate the BEFORE
    # arm. SQLite and all queries are real; only connection bootstrap is bypassed.
    def connect():
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(store, "_connect", connect)
    monkeypatch.setattr(commands, "_current_channel", lambda: ("telegram", "chat-1"))
    return target


def outputs(db):
    with sqlite3.connect(db) as conn:
        data = {
            "tokens.ace": conn.execute((FIXTURES / "tokens_overall.sql").read_text(), (0,)).fetchall(),
            "daily-journal": conn.execute(JOURNAL, (0, 10000)).fetchall(),
            "reprice": conn.execute(REPRICE).fetchall(),
            "reprice_rollback": [conn.execute(q).fetchall() for q in REPRICE_PROBES],
        }
    data["/cost"] = commands._handle_latest()
    rec = last_turn.compute_last_turn_record("telegram", "chat-1")
    assert rec["found"]
    data["/context"] = last_turn.render_last_turn_record(rec)
    assert len(data["daily-journal"]) == 500
    assert data["tokens.ace"][0][0] == 500
    assert data["reprice"]
    return json.dumps(data, sort_keys=True, ensure_ascii=False).encode()


def test_ac7_populated_migration_preserves_five_consumers(legacy_copy):
    before = outputs(legacy_copy)
    with sqlite3.connect(legacy_copy) as conn:
        old_columns = [r[1] for r in conn.execute("PRAGMA table_info(turns)")]
        old_rows = conn.execute("SELECT * FROM turns ORDER BY turn_id").fetchall()
        store._ensure_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM turns WHERE served_subs_json IS NOT NULL OR attribution IS NOT NULL").fetchone()[0] == 0
        conn.execute("UPDATE turns SET served_subs_json = ?, attribution = 'wire'", ('{"sub-vps-7":1}',))
        ids = [r[0] for r in conn.execute("SELECT turn_id FROM turns")]
    for turn_id in ids:
        store.insert_api_call(turn_id, 0, ts=1500, provider="claude-apr", model="test-model",
                              usage=CanonicalUsage(1000, 50, 300, 20, 7),
                              sub_key="sub-vps-7", attribution="wire")
    with sqlite3.connect(legacy_copy) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turn_api_calls").fetchone()[0] == 500
        assert conn.execute(f"SELECT {','.join(old_columns)} FROM turns ORDER BY turn_id").fetchall() == old_rows
        migrated = list(conn.iterdump())
        store._ensure_schema(conn)
        assert list(conn.iterdump()) == migrated
    # The frozen old queries and unchanged readers still work on the new DB.
    assert outputs(legacy_copy) == before


def test_insert_turn_placeholder_probe_detects_malformed_insert(legacy_copy, monkeypatch):
    class BadInsert(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "INSERT OR REPLACE INTO turns" in sql:
                sql = sql.replace("?, ?", "?", 1)
            return super().execute(sql, parameters)

    def bad_connect():
        conn = sqlite3.connect(legacy_copy, factory=BadInsert)
        conn.row_factory = sqlite3.Row
        return conn

    with monkeypatch.context() as m:
        m.setattr(store, "_connect", bad_connect)
        store.insert_turn(TurnRecord(turn_id="probe"))
        # insert_turn catches telemetry failures; absence is the actual symptom.
        with pytest.raises(AssertionError):
            assert store.get_turn("probe") is not None
    store.insert_turn(TurnRecord(turn_id="probe"))
    assert store.get_turn("probe") is not None
