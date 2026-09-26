"""first_call_cache_miss is lane-aware: read-only lanes classify by read share.

xai-oauth (and any OpenAI-shaped usage carrying only
prompt_tokens_details.cached_tokens) never reports a cache write, so the
Anthropic write rule (cache_write*5 >= 4*total) is structurally 0 there.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from plugins import blackbox
from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {"enabled": True})
    with store._connect():
        pass
    return store._db_path()


def _record(turn, provider, usage, api_mode):
    blackbox.record_api_call(turn_id=turn, seq=0, ts=1, provider=provider, model="m",
                             usage=usage, api_mode=api_mode, sub_key=None,
                             attribution="wire", http_status=200,
                             relay_synthetic=False, route_id=None)
    store.insert_turn(TurnRecord(turn_id=turn, chat_id=turn, ts_start=1, ts_end=2))


def _openai_usage(prompt, cached):
    """Parsed through the real OpenAI SDK model, as the chat transport does."""
    from openai.types import CompletionUsage
    return CompletionUsage.model_validate({
        "prompt_tokens": prompt, "completion_tokens": 50, "total_tokens": prompt + 50,
        "prompt_tokens_details": {"cached_tokens": cached}})


def _miss(db, turn):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT first_call_cache_miss FROM turns WHERE turn_id=?",
                            (turn,)).fetchone()[0]


def test_xai_cold_first_call_flags_miss(db):
    # Live case (t_f77c412e): xai-oauth call 0 read 1,152 of 356k; flag read 0.
    _record("xai_cold", "xai-oauth", _openai_usage(356_000, 1_152), "chat_completions")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT lane_family, cache_write FROM turn_api_calls "
                            "WHERE turn_id='xai_cold'").fetchone() == ("xai", 0)
    assert _miss(db, "xai_cold") == 1


def test_xai_warm_first_call_is_not_a_miss(db):
    _record("xai_warm", "xai-oauth", _openai_usage(356_000, 355_000), "chat_completions")
    assert _miss(db, "xai_warm") == 0


def test_codex_cached_tokens_only_lane_is_read_share_classified(db):
    _record("codex_cold", "openai-codex", _openai_usage(100_000, 0), "chat_completions")
    _record("codex_warm", "openai-codex", _openai_usage(100_000, 90_000), "chat_completions")
    assert (_miss(db, "codex_cold"), _miss(db, "codex_warm")) == (1, 0)


def _anthropic(inp, read, write):
    return SimpleNamespace(input_tokens=inp, output_tokens=5,
                           cache_read_input_tokens=read,
                           cache_creation_input_tokens=write)


@pytest.mark.parametrize("usage,expected", [
    (_anthropic(100, 0, 50_000), 1),     # cold write: miss (unchanged)
    (_anthropic(100, 90_000, 500), 0),   # warm read: not a miss (unchanged)
    (_anthropic(100, 0, 0), 0),          # no read, no write: write rule says 0
])
def test_anthropic_lanes_keep_the_write_rule(db, usage, expected):
    for provider in ("claude-apr", "claude-bpr", "claude-cpr"):
        turn = f"{provider}-{expected}-{usage.cache_read_input_tokens}"
        _record(turn, provider, usage, "anthropic_messages")
        assert _miss(db, turn) == expected, provider
