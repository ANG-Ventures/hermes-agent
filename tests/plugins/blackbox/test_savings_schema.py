from __future__ import annotations

import inspect
import re
import sqlite3

import pytest

from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord


def _record(turn_id: str = "turn-savings", **overrides) -> TurnRecord:
    values = {
        "turn_id": turn_id,
        "ts_start": 1.0,
        "ts_end": 2.0,
        "profile": "default",
        "provider": "openai",
        "model": "gpt-test",
        "platform": "telegram",
        "chat_id": "chat-1",
        "api_calls": 1,
        "tools": ["terminal"],
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "context_used": 100,
        "context_length": 1000,
        "turn_saved_tokens_est": 12,
        "tool_calls": [
            {
                "name": "terminal",
                "args_preview": '{"command":"rtk ls"}',
                "result_preview": "compact output",
                "saved_bytes": 48,
                "saved_tokens_est": 12,
                "compressor": "rtk",
                "raw_bytes_source": "rtk-selfreport",
            }
        ],
    }
    values.update(overrides)
    return TurnRecord(**values)


@pytest.fixture
def bb_store(tmp_path, monkeypatch):
    db = tmp_path / "turns.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    return store


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_savings_columns_round_trip_and_native_exact_source(bb_store):
    bb_store.insert_turn(
        _record(
            turn_id="turn-native",
            turn_saved_tokens_est=33,
            tool_calls=[
                {
                    "name": "terminal",
                    "args_preview": "{}",
                    "result_preview": "{}",
                    "saved_bytes": 132,
                    "saved_tokens_est": 33,
                    "compressor": "native",
                    "raw_bytes_source": "native-exact",
                }
            ],
        )
    )

    row = bb_store.get_turn("turn-native")
    assert row is not None
    assert row["turn_saved_tokens_est"] == 33

    with bb_store._connect() as conn:
        call = conn.execute(
            """
            SELECT saved_bytes, saved_tokens_est, compressor, raw_bytes_source
            FROM turn_tool_calls
            WHERE turn_id = ? AND seq = 0
            """,
            ("turn-native",),
        ).fetchone()
    assert dict(call) == {
        "saved_bytes": 132,
        "saved_tokens_est": 33,
        "compressor": "native",
        "raw_bytes_source": "native-exact",
    }


def test_absent_savings_stay_null_not_zero(bb_store):
    bb_store.insert_turn(
        _record(
            turn_id="turn-no-savings",
            turn_saved_tokens_est=None,
            tool_calls=[{"name": "terminal", "args_preview": "{}", "result_preview": "raw"}],
        )
    )

    row = bb_store.get_turn("turn-no-savings")
    assert row["turn_saved_tokens_est"] is None
    with bb_store._connect() as conn:
        call = conn.execute(
            """
            SELECT saved_bytes, saved_tokens_est, compressor, raw_bytes_source
            FROM turn_tool_calls
            WHERE turn_id = ? AND seq = 0
            """,
            ("turn-no-savings",),
        ).fetchone()
    assert dict(call) == {
        "saved_bytes": None,
        "saved_tokens_est": None,
        "compressor": None,
        "raw_bytes_source": None,
    }
    assert ("—" if row["turn_saved_tokens_est"] is None else str(row["turn_saved_tokens_est"])) == "—"


def test_savings_migration_adds_nullable_columns_and_is_idempotent(bb_store):
    db = bb_store._db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE turns ("
        "turn_id TEXT PRIMARY KEY, platform TEXT, chat_id TEXT, ts_end REAL, "
        "cost_usd REAL)"
    )
    conn.execute(
        "CREATE TABLE turn_tool_calls ("
        "turn_id TEXT, seq INT, name TEXT, args_preview TEXT, result_preview TEXT, "
        "PRIMARY KEY(turn_id, seq))"
    )
    conn.execute(
        "INSERT INTO turns (turn_id, platform, chat_id, ts_end, cost_usd) "
        "VALUES ('legacy', 'cli', 'chat', 1.0, 0.0)"
    )
    conn.execute(
        "INSERT INTO turn_tool_calls (turn_id, seq, name, args_preview, result_preview) "
        "VALUES ('legacy', 0, 'terminal', '{}', 'old')"
    )
    conn.commit()
    conn.close()

    with bb_store._connect() as migrated:
        assert "turn_saved_tokens_est" in _columns(migrated, "turns")
        for col in ("saved_bytes", "saved_tokens_est", "compressor", "raw_bytes_source"):
            assert col in _columns(migrated, "turn_tool_calls")
        old_turn = migrated.execute(
            "SELECT turn_saved_tokens_est FROM turns WHERE turn_id = 'legacy'"
        ).fetchone()
        old_call = migrated.execute(
            """
            SELECT saved_bytes, saved_tokens_est, compressor, raw_bytes_source
            FROM turn_tool_calls
            WHERE turn_id = 'legacy' AND seq = 0
            """
        ).fetchone()
    assert old_turn["turn_saved_tokens_est"] is None
    assert dict(old_call) == {
        "saved_bytes": None,
        "saved_tokens_est": None,
        "compressor": None,
        "raw_bytes_source": None,
    }

    with bb_store._connect() as migrated_again:
        assert "turn_saved_tokens_est" in _columns(migrated_again, "turns")


def test_raw_bytes_source_enum_accepts_native_exact_and_rejects_unknown(bb_store):
    with bb_store._connect() as conn:
        conn.execute(
            """
            INSERT INTO turn_tool_calls (
                turn_id, seq, name, args_preview, result_preview, raw_bytes_source
            ) VALUES ('ok', 0, 'terminal', '{}', '{}', 'native-exact')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO turn_tool_calls (
                    turn_id, seq, name, args_preview, result_preview, raw_bytes_source
                ) VALUES ('bad', 0, 'terminal', '{}', '{}', 'unknown')
                """
            )


def test_insert_placeholder_counts_match_column_lists():
    src = inspect.getsource(store.insert_turn)
    turns = re.search(
        r"INSERT OR REPLACE INTO turns \((?P<cols>.*?)\) VALUES \((?P<vals>.*?)\)",
        src,
        re.S,
    )
    calls = re.search(
        r"INSERT INTO turn_tool_calls \((?P<cols>.*?)\) VALUES \((?P<vals>.*?)\)",
        src,
        re.S,
    )
    assert turns is not None
    assert calls is not None

    for match in (turns, calls):
        cols = [c.strip() for c in match.group("cols").split(",") if c.strip()]
        placeholders = re.findall(r"\?", match.group("vals"))
        assert len(placeholders) == len(cols)
