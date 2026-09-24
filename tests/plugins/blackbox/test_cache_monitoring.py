"""Cache monitoring is nullable, joinable and reproducible across migrations."""
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from plugins import blackbox
from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord
from agent.chat_completion_helpers import _requested_cache_ttl, _record_successful_api_call


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {"enabled": True})
    with store._connect():
        pass
    return store._db_path()


def test_tier_split_is_native_only_and_zero_is_not_missing(db):
    native = SimpleNamespace(input_tokens=100, output_tokens=5,
                             cache_creation_input_tokens=800,
                             cache_creation=SimpleNamespace(
                                 ephemeral_5m_input_tokens=0,
                                 ephemeral_1h_input_tokens=800))
    for seq, provider, usage in [(0, "claude-bpr", native),
                                 (1, "openai-codex", SimpleNamespace(input_tokens=100,
                                                                       output_tokens=5))]:
        blackbox.record_api_call(turn_id="t", seq=seq, ts=seq + 1,
                                 provider=provider, model="m", usage=usage,
                                 api_mode="anthropic_messages" if seq == 0 else "codex_responses",
                                 sub_key="s" if seq == 0 else None, attribution="wire",
                                 http_status=200, relay_synthetic=False, route_id=None,
                                 cache_ttl_requested="1h" if seq == 0 else None)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT lane_family, cache_write_5m, cache_write_1h, "
                            "cache_ttl_requested FROM turn_api_calls ORDER BY seq").fetchall() == [
                                ("bpx/bpr", 0, 800, "1h"), ("codex", None, None, None)]


def test_ttl_is_measured_from_request_markers_not_config():
    assert _requested_cache_ttl({"system": [{"cache_control": {"type": "ephemeral", "ttl": "1h"}}]}) == "1h"
    assert _requested_cache_ttl({"messages": [{"content": [{"cache_control": {"type": "ephemeral"}}]}]}) == "5m"
    assert _requested_cache_ttl({"messages": []}) is None


def test_chokepoint_writes_native_split_and_requested_ttl(db):
    agent = SimpleNamespace(_current_turn_id="wire-turn", provider="claude-bpx-2",
                            model="m", api_mode="anthropic_messages")
    response = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=2,
                              cache_creation_input_tokens=80,
                              cache_creation={"ephemeral_5m_input_tokens": 0,
                                              "ephemeral_1h_input_tokens": 80}),
        pool_headers={"x-pool-served-by": "sub-vps-2"})
    _record_successful_api_call(agent, response, {
        "system": [{"cache_control": {"type": "ephemeral", "ttl": "1h"}}]})
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT cache_write_5m,cache_write_1h,cache_ttl_requested,sub_key "
                            "FROM turn_api_calls WHERE turn_id='wire-turn'").fetchone() == (
                                0, 80, "1h", "sub-vps-2")


def test_turn_gap_first_successful_call_and_nullable_rollup(db):
    store.insert_turn(TurnRecord(turn_id="old", chat_id="chat", ts_start=10, ts_end=20))
    usage = SimpleNamespace(input_tokens=100, output_tokens=5,
                            cache_creation_input_tokens=900,
                            cache_creation={"ephemeral_5m_input_tokens": 900,
                                            "ephemeral_1h_input_tokens": 0})
    blackbox.record_api_call(turn_id="new", seq=0, ts=35, provider="claude-apr",
                             model="m", usage=usage, api_mode="anthropic_messages",
                             sub_key="sub-vps-7", attribution="wire", http_status=200,
                             relay_synthetic=False, route_id=None)
    store.insert_turn(TurnRecord(turn_id="new", chat_id="chat", ts_start=35,
                                 ts_end=40, context_used=1000))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT gap_prev_turn_s, first_call_cache_miss, "
                            "cache_write_5m, cache_write_1h FROM turns WHERE turn_id='new'").fetchone() == (
                                15, 1, 900, 0)
        assert conn.execute("SELECT gap_prev_turn_s, first_call_cache_miss FROM turns "
                            "WHERE turn_id='old'").fetchone() == (None, None)
    # Re-finalize must not erase the per-call rollup.
    store.insert_turn(TurnRecord(turn_id="new", chat_id="chat", ts_start=35,
                                 ts_end=40, context_used=1000))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT gap_prev_turn_s, cache_write_5m FROM turns "
                            "WHERE turn_id='new'").fetchone() == (15, 900)


def test_backfill_only_derivable_history_and_is_idempotent(db):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO turns(turn_id, chat_id, ts_start, ts_end, context_used) "
                     "VALUES ('a', 'x', 1, 4, 1000), ('b', 'x', 14, 20, 1000), "
                     "('other', 'y', 10, 12, 1000)")
        conn.execute("INSERT INTO turn_api_calls(turn_id,seq,ts,provider,input_tokens,cache_read,cache_write) "
                     "VALUES ('b',0,15,'claude-apr',100,0,900)")
    store.backfill_cache_monitoring()
    store.backfill_cache_monitoring()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT turn_id,gap_prev_turn_s,first_call_cache_miss "
                            "FROM turns ORDER BY turn_id").fetchall() == [
                                ("a", None, None), ("b", 10, 1), ("other", None, None)]
        assert conn.execute("SELECT lane_family,cache_write_5m,cache_write_1h "
                            "FROM turn_api_calls").fetchone() == ("apx/apr", None, None)


def test_existing_call_table_migrates_without_inventing_tiers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = store._db_path()
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE turn_api_calls (turn_id TEXT NOT NULL, seq INT NOT NULL, "
                     "ts REAL, sub_key TEXT, provider TEXT, cache_write INT, "
                     "PRIMARY KEY(turn_id,seq))")
        conn.execute("INSERT INTO turn_api_calls(turn_id,seq,provider,cache_write) "
                     "VALUES ('old',0,'xai-oauth',100)")
    with store._connect():
        pass
    with sqlite3.connect(db) as conn:
        for col in ("cache_write_5m", "cache_write_1h", "cache_ttl_requested", "lane_family"):
            assert col in {row[1] for row in conn.execute("PRAGMA table_info(turn_api_calls)")}
        assert conn.execute("SELECT cache_write_5m,cache_write_1h FROM turn_api_calls").fetchone() == (None, None)
    store.backfill_cache_monitoring()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT lane_family,cache_write_5m,cache_write_1h FROM turn_api_calls").fetchone() == (
            "xai", None, None)


def test_compaction_metadata_is_persisted_without_inventing_cost(db):
    record = blackbox._build_record(
        session_id="c", interrupted=False, model="m", platform="cli",
        provider="claude-apr", user_message="", final_response="",
        turn_usage={"idle_compaction_fired": True, "compaction_tokens_before": 2200,
                    "compaction_tokens_after": 300, "compaction_cost_usd": 0.012},
        cfg={"record_subagents": True, "store_text": False}, kwargs={"turn_id": "c:1"})
    store.insert_turn(record)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT idle_compaction_fired,compaction_tokens_before,"
                            "compaction_tokens_after,compaction_cost_usd FROM turns "
                            "WHERE turn_id='c:1'").fetchone() == (1, 2200, 300, 0.012)


def test_compaction_price_is_unknown_without_provider_usage():
    from agent.context_compressor import _priced_summary_call
    assert _priced_summary_call(None, provider="claude-apr", model="claude-opus-5") is None
    measured = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
        cache_creation_input_tokens=0))
    assert _priced_summary_call(measured, provider="claude-apr",
                                model="claude-opus-4-6", api_mode="anthropic_messages") == 0.0075


def test_only_committed_idle_compaction_is_attributed_to_current_turn():
    from agent.conversation_compression import _record_blackbox_compaction
    agent = SimpleNamespace(_blackbox_compaction={"idle_compaction_fired": False})
    _record_blackbox_compaction(agent, trigger="idle_resume", before=2000, after=300,
                                telemetry={"aux_cost_usd": 0.01})
    assert agent._blackbox_compaction == {
        "idle_compaction_fired": True, "compaction_tokens_before": 2000,
        "compaction_tokens_after": 300, "compaction_cost_usd": 0.01}
    _record_blackbox_compaction(agent, trigger="threshold", before=1800, after=280,
                                telemetry={"aux_cost_usd": 0.02})
    assert agent._blackbox_compaction["compaction_cost_usd"] == 0.03
    assert agent._blackbox_compaction["idle_compaction_fired"] is True
    _record_blackbox_compaction(agent, trigger="threshold", before=1800, after=280,
                                telemetry={"chunking": True, "aux_cost_usd": 0.02})
    assert agent._blackbox_compaction["compaction_cost_usd"] is None
    # A subsequent partial/unknown price cannot resurrect a false total.
    _record_blackbox_compaction(agent, trigger="threshold", before=1800, after=280,
                                telemetry={"aux_cost_usd": 0.02})
    assert agent._blackbox_compaction["compaction_cost_usd"] is None


def test_backfill_cli_dryrun_then_backup_and_apply(db, monkeypatch, capsys):
    from scripts import backfill_blackbox_cache_monitoring as cli
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO turn_api_calls(turn_id,seq,provider,input_tokens,cache_read,cache_write) "
                     "VALUES ('t',0,'claude-bpr',100,0,900)")
    monkeypatch.setattr(sys, "argv", ["backfill", "--home", str(db.parent.parent)])
    cli.main()
    assert "mode=dry-run" in capsys.readouterr().out
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT lane_family FROM turn_api_calls").fetchone()[0] is None
    monkeypatch.setattr(sys, "argv", ["backfill", "--home", str(db.parent.parent), "--apply"])
    cli.main()
    assert "verified: lane_family=1" in capsys.readouterr().out
    assert len(list(db.parent.glob("turns-before-cache-backfill-*.db"))) == 1


# --- r2 (Argus F1/F3) -------------------------------------------------------

# RECORDED: the bpr lane's streamed chat-completion usage as captured live by
# Argus (probe_tiers_opus.out, 2026-09-24): the bridge flattened the split.
_BPR_USAGE_PRE_SPLIT = {
    "prompt_tokens": 20975, "completion_tokens": 12, "total_tokens": 20987,
    "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 20970},
}
# The same shape after claude-bpx emits the CLI's own split (bridge change in
# the linked claude-bpx PR); inner keys are the CLI's recorded usage.cache_creation.
_BPR_USAGE_WITH_SPLIT = {
    **_BPR_USAGE_PRE_SPLIT,
    "prompt_tokens_details": {
        "cached_tokens": 0, "cache_creation_tokens": 20970,
        "cache_creation": {"ephemeral_5m_input_tokens": 0,
                           "ephemeral_1h_input_tokens": 20970},
    },
}


def _sdk_usage(payload):
    """Parse through the real OpenAI SDK model, as the chat transport does."""
    from openai.types import CompletionUsage
    return CompletionUsage.model_validate(payload)


def test_bpr_openai_shaped_split_is_recorded_and_flattened_shape_stays_null(db):
    for seq, payload in enumerate((_BPR_USAGE_WITH_SPLIT, _BPR_USAGE_PRE_SPLIT)):
        blackbox.record_api_call(turn_id="bpr", seq=seq, ts=seq + 1, provider="claude-bpr",
                                 model="claude-opus-5-5", usage=_sdk_usage(payload),
                                 api_mode="chat_completions", sub_key="sub-vps-16",
                                 attribution="wire", http_status=200,
                                 relay_synthetic=False, route_id=None)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT lane_family, cache_write, cache_write_5m, cache_write_1h "
                            "FROM turn_api_calls WHERE turn_id='bpr' ORDER BY seq").fetchall() == [
                                ("bpx/bpr", 20970, 0, 20970), ("bpx/bpr", 20970, None, None)]


def _call(turn, seq, status, write, inp=100, with_usage=None):
    if with_usage is None:
        with_usage = status == 200
    usage = (SimpleNamespace(input_tokens=inp, output_tokens=5, cache_read_input_tokens=0,
                             cache_creation_input_tokens=write) if with_usage else None)
    blackbox.record_api_call(turn_id=turn, seq=seq, ts=seq + 1, provider="claude-apr",
                             model="m", usage=usage, api_mode="anthropic_messages",
                             sub_key=None, attribution="wire", http_status=status,
                             relay_synthetic=False, route_id=None)


def test_first_call_miss_skips_a_failed_first_attempt(db):
    # 429 (zero usage) then a cold 50k write: the classification belongs to the
    # first SUCCESSFUL call. Control: the same turn without the failed attempt.
    _call("retry", 0, 429, 0)
    _call("retry", 1, 200, 50_000)
    _call("control", 0, 200, 50_000)
    for tid in ("retry", "control"):
        store.insert_turn(TurnRecord(turn_id=tid, chat_id=tid, ts_start=1, ts_end=2))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT turn_id, first_call_cache_miss FROM turns "
                            "WHERE turn_id IN ('retry','control') ORDER BY turn_id").fetchall() == [
                                ("control", 1), ("retry", 1)]


def test_first_call_miss_filters_status_and_empty_usage_independently(db):
    # A failed attempt that still reported usage (e.g. a 529 mid-stream with a
    # warm read) must not classify the turn: the status filter alone decides.
    _call("err_with_usage", 0, 529, 0, inp=100, with_usage=True)
    _call("err_with_usage", 1, 200, 50_000)
    # A status-less attempt with an all-zero usage (synthetic/empty) must not
    # classify the turn either: the usage filter alone decides.
    _call("empty_ok", 0, None, 0, inp=0, with_usage=True)
    _call("empty_ok", 1, 200, 50_000)
    for tid in ("err_with_usage", "empty_ok"):
        store.insert_turn(TurnRecord(turn_id=tid, chat_id=tid, ts_start=1, ts_end=2))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT turn_id, first_call_cache_miss FROM turns WHERE turn_id "
                            "IN ('err_with_usage','empty_ok') ORDER BY turn_id").fetchall() == [
                                ("empty_ok", 1), ("err_with_usage", 1)]


@pytest.mark.parametrize("first_row", [
    # zero-usage failure (the live case: 55 argus turns)
    "('h',0,1,'claude-apr',429,0,0,0)",
    # a failed attempt that still carried partial usage: status alone must exclude it
    "('h',0,1,'claude-apr',529,100,0,0)",
    # a 200 with no usage (relay-synthetic / empty): usage alone must exclude it
    "('h',0,1,'claude-apr',200,0,0,0)",
])
def test_backfill_first_call_miss_uses_first_successful_measured_call(db, first_row):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO turns(turn_id, chat_id, ts_start, ts_end) VALUES ('h','c',1,2)")
        conn.execute("INSERT INTO turn_api_calls(turn_id,seq,ts,provider,http_status,"
                     "input_tokens,cache_read,cache_write) VALUES "
                     f"{first_row}, ('h',1,2,'claude-apr',200,100,0,50000)")
    store.backfill_cache_monitoring()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT first_call_cache_miss FROM turns WHERE turn_id='h'").fetchone() == (1,)


def test_multiple_compactions_keep_first_before_last_after():
    from agent.conversation_compression import _record_blackbox_compaction
    agent = SimpleNamespace(_blackbox_compaction={"idle_compaction_fired": False})
    _record_blackbox_compaction(agent, trigger="threshold", before=5000, after=900,
                                telemetry=None, cost_sink={"usd": 0.01, "calls": 1, "unknown": False})
    _record_blackbox_compaction(agent, trigger="threshold", before=4000, after=700,
                                telemetry=None, cost_sink={"usd": 0.02, "calls": 2, "unknown": False})
    state = agent._blackbox_compaction
    assert (state["compaction_tokens_before"], state["compaction_tokens_after"],
            state["compaction_cost_usd"]) == (5000, 700, 0.03)
    # An unpriced sink poisons the total; it is never silently summed.
    _record_blackbox_compaction(agent, trigger="threshold", before=3000, after=600,
                                telemetry={"aux_cost_usd": 0.5},
                                cost_sink={"usd": 0.0, "calls": 1, "unknown": True})
    assert state["compaction_cost_usd"] is None
