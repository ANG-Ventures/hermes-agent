"""A turn whose billed main-lane calls span several routes is stamped ``mixed`` (t_a24c429c).

turns.provider/model name the turn's primary route, but the token columns sum
every call. A codex turn with claude-bpr fallback calls used to claim
lane_family='codex' / served_provider='openai-codex' while carrying the Claude
cache writes. The route columns must let a reader tell that turn apart.
"""
import sqlite3

import pytest

from agent.usage_pricing import CanonicalUsage
from plugins import blackbox
from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord

_HOME_ENV = "HERMES" + "_HOME"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv(_HOME_ENV, str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {"enabled": True})
    with store._connect():
        pass
    return store._db_path()


def _call(turn_id, seq, provider, model, *, attribution="wire", **usage):
    store.insert_api_call(
        turn_id, seq, ts=seq + 1, provider=provider, model=model,
        usage=CanonicalUsage(**usage), sub_key=None, attribution=attribution,
        http_status=200 if usage else 429,
    )


def _turn(turn_id, provider, model, **tokens):
    store.insert_turn(TurnRecord(turn_id=turn_id, chat_id="c", ts_start=1, ts_end=9,
                                 provider=provider, model=model, **tokens))


def _route(db, turn_id):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT provider, lane_family, vendor, served_provider FROM turns "
            "WHERE turn_id = ?", (turn_id,)).fetchone()


def test_codex_turn_with_bpr_fallback_is_mixed(db):
    _call("t_mix", 0, "openai-codex", "gpt-5.5", input_tokens=900, output_tokens=50,
          cache_read_tokens=100)
    _call("t_mix", 1, "claude-bpr", "claude-opus-4-8", input_tokens=10,
          output_tokens=40, cache_write_tokens=5_000)
    _turn("t_mix", "openai-codex", "gpt-5.5", input_tokens=910, output_tokens=90,
          cache_read_tokens=100, cache_write_tokens=5_000)
    # provider keeps naming the primary; the route columns say mixed.
    assert _route(db, "t_mix") == ("openai-codex", "mixed", "mixed", "mixed")


def test_single_provider_turn_is_unchanged(db):
    _call("t_one", 0, "openai-codex", "gpt-5.5", input_tokens=900, output_tokens=50)
    _call("t_one", 1, "openai-codex", "gpt-5.5", input_tokens=950, output_tokens=60)
    _turn("t_one", "openai-codex", "gpt-5.5", input_tokens=1_850, output_tokens=110)
    assert _route(db, "t_one") == ("openai-codex", "codex", "openai", "openai-codex")


def test_turn_without_ledger_rows_is_unchanged(db):
    _turn("t_none", "claude-bpr", "claude-opus-4-8", input_tokens=10)
    assert _route(db, "t_none") == ("claude-bpr", "bpx/bpr", "anthropic", "claude-bpr")


def test_fallback_call_arriving_after_the_turn_row_marks_it_mixed(db):
    _call("t_late", 0, "openai-codex", "gpt-5.5", input_tokens=900, output_tokens=50)
    _turn("t_late", "openai-codex", "gpt-5.5", input_tokens=900, output_tokens=50)
    assert _route(db, "t_late")[1] == "codex"
    _call("t_late", 1, "claude-apr", "claude-opus-4-8", input_tokens=5,
          cache_write_tokens=2_000)
    assert _route(db, "t_late") == ("openai-codex", "mixed", "mixed", "mixed")
    # A re-finalize of the same turn keeps the honest stamp.
    _turn("t_late", "openai-codex", "gpt-5.5", input_tokens=905, output_tokens=50,
          cache_write_tokens=2_000)
    assert _route(db, "t_late")[1:] == ("mixed", "mixed", "mixed")


def test_primary_failed_with_zero_usage_and_fallback_served_all_is_mixed(db):
    # The turn row still says codex, yet every token came from claude-bpr.
    _call("t_fb", 0, "openai-codex", "gpt-5.5")  # 429, no usage
    _call("t_fb", 1, "claude-bpr", "claude-opus-4-8", input_tokens=10,
          cache_write_tokens=3_000)
    _turn("t_fb", "openai-codex", "gpt-5.5", input_tokens=10, cache_write_tokens=3_000)
    assert _route(db, "t_fb")[1:] == ("mixed", "mixed", "mixed")


def test_zero_usage_failed_fallback_does_not_mark_mixed(db):
    _call("t_try", 0, "claude-bpr", "claude-opus-4-8")  # failed attempt, no usage
    _call("t_try", 1, "openai-codex", "gpt-5.5", input_tokens=900, output_tokens=50)
    _turn("t_try", "openai-codex", "gpt-5.5", input_tokens=900, output_tokens=50)
    assert _route(db, "t_try") == ("openai-codex", "codex", "openai", "openai-codex")


def test_two_claude_lanes_mix_lane_but_keep_vendor(db):
    _call("t_cl", 0, "claude-apr", "claude-opus-4-8", input_tokens=10, output_tokens=5)
    _call("t_cl", 1, "claude-bpr", "claude-opus-4-8", input_tokens=10, output_tokens=5)
    _turn("t_cl", "claude-apr", "claude-opus-4-8", input_tokens=20, output_tokens=10)
    assert _route(db, "t_cl") == ("claude-apr", "mixed", "anthropic", "mixed")


def test_aux_calls_never_make_a_turn_mixed(db):
    _call("t_aux", 0, "openai-codex", "gpt-5.5", input_tokens=900, output_tokens=50)
    _call("t_aux", 1, "claude-bpr", "claude-haiku-4-5", attribution="aux:compression",
          input_tokens=4_000, output_tokens=300)
    _turn("t_aux", "openai-codex", "gpt-5.5", input_tokens=900, output_tokens=50)
    assert _route(db, "t_aux") == ("openai-codex", "codex", "openai", "openai-codex")


def test_same_lane_different_subs_is_not_mixed(db):
    _call("t_sub", 0, "claude-bpx-3", "claude-opus-4-8", input_tokens=10, output_tokens=5)
    _call("t_sub", 1, "claude-bpx-16", "claude-opus-4-8", input_tokens=10, output_tokens=5)
    _turn("t_sub", "claude-bpx-3", "claude-opus-4-8", input_tokens=20, output_tokens=10)
    row = _route(db, "t_sub")
    assert row[1:3] == ("bpx/bpr", "anthropic")
