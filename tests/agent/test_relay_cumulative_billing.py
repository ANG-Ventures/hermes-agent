"""Multi-request relay responses bill every request's prompt (t_9f190074, claude-bpx#564).

The wire payloads are claude-bpx#564's AU-3 shape (bridge/test/unit-tui-arrival-usage.test.js),
built through the openai SDK's own response constructor so the relay fields arrive as
``model_extra`` exactly as they do live:

- the arrival publish: ``usage`` all null + ``unavailable`` + ``usage_deferred: true``;
- the next response of the turn: prompt = the LAST request's own prompt (214,822), output summed,
  ``upstream_requests: 2``, ``cumulative_prompt_tokens_details`` = the billed sum by field.

Contract: the bill (session counters, cost, Blackbox token columns) is the cumulative split; the
context size (compressor) stays the last request; the deferred row bills zero, marked as deferred,
not unknown, and nothing is counted twice.
"""
import sqlite3

import pytest
from openai._models import construct_type
from openai.types.chat import ChatCompletion

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

LAST = {"input_tokens": 2, "cache_read_tokens": 213_805, "cache_write_tokens": 1_015}
BILLED = {"input_tokens": 4, "cache_read_tokens": 426_128, "cache_write_tokens": 2_497}
OUTPUT = 669

_DEFERRED_USAGE = {
    "prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "unavailable": True,
    "prompt_tokens_unavailable": True, "output_tokens_unavailable": True,
    "total_tokens_unavailable": True,
    "prompt_tokens_details": {"cached_tokens": None, "cache_creation_tokens": None},
    "usage_deferred": True,
}
_CARRYING_USAGE = {
    "prompt_tokens": sum(LAST.values()), "completion_tokens": OUTPUT,
    "total_tokens": sum(LAST.values()) + OUTPUT,
    "prompt_tokens_details": {"cached_tokens": LAST["cache_read_tokens"],
                              "cache_creation_tokens": LAST["cache_write_tokens"]},
    "upstream_requests": 2, "carried_requests": 1, "cumulative_prompt_tokens": sum(BILLED.values()),
    "cumulative_prompt_tokens_details": {"input_tokens": BILLED["input_tokens"],
                                         "cached_tokens": BILLED["cache_read_tokens"],
                                         "cache_creation_tokens": BILLED["cache_write_tokens"]},
}


def _response(usage):
    return construct_type(type_=ChatCompletion, value={
        "id": "chatcmpl-r", "object": "chat.completion", "created": 1, "model": "claude-fable-5.1",
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": ""}}],
        "usage": usage,
    })


def _agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    a = AIAgent(api_key="k", base_url="https://inference-api.nousresearch.com/v1", provider="nous",
                api_mode="chat_completions", model="anthropic/claude-fable-5.1", session_id="t",
                platform="cli", quiet_mode=True, skip_context_files=True, skip_memory=True,
                save_trajectories=False, enabled_toolsets=["file"])
    a.context_compressor.context_length = 1_000_000
    return a


def _record(agent, resp, n, turn_calls):
    from agent import turn_usage

    msgs = [{"role": "user", "content": "go"}]
    turn_usage.record_response_usage(agent, resp, messages=msgs, api_call_count=n, api_duration=1.0,
                                     compression_attempts=0, max_compression_attempts=3,
                                     turn_calls=turn_calls)


def test_agent_bills_the_cumulative_split_and_keeps_last_request_context(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    try:
        calls: list = []
        _record(a, _response(_DEFERRED_USAGE), 1, calls)
        # Deferred is not unknown: the session stays fully measured, nothing billed yet.
        assert not any(getattr(a, f"session_{k}", False) for k in (
            "input_tokens_unknown", "output_tokens_unknown", "cache_read_tokens_unknown",
            "cache_write_tokens_unknown", "usage_unknown"))
        assert a.session_cache_read_tokens == 0 and a.session_estimated_cost_usd == 0

        _record(a, _response(_CARRYING_USAGE), 2, calls)
        assert a.session_input_tokens == BILLED["input_tokens"]
        assert a.session_cache_read_tokens == BILLED["cache_read_tokens"]
        assert a.session_cache_write_tokens == BILLED["cache_write_tokens"]
        assert a.session_output_tokens == OUTPUT
        assert a.session_prompt_tokens == sum(BILLED.values())
        expected = estimate_usage_cost(
            "anthropic/claude-fable-5.1", CanonicalUsage(output_tokens=OUTPUT, **BILLED),
            provider="nous", base_url="https://inference-api.nousresearch.com/v1", api_key="k")
        assert expected.amount_usd
        assert a.session_estimated_cost_usd == pytest.approx(float(expected.amount_usd))
        # The Blackbox turn rollup sums these: billed once, never the deferred row's nothing twice.
        assert sum(c["cache_read_tokens"] for c in calls) == BILLED["cache_read_tokens"]
        assert sum(c["cache_write_tokens"] for c in calls) == BILLED["cache_write_tokens"]
        # Context size stays the last request's own prompt.
        assert a.context_compressor.last_prompt_tokens == sum(LAST.values())
    finally:
        a.close()


class _MoaClient:
    """An MoA client whose advisors spent ``REF`` tokens on this call."""

    def __init__(self, ref):
        self._ref = ref

    def consume_reference_usage(self):
        ref, self._ref = self._ref, None
        return ref, None


REF = CanonicalUsage(input_tokens=10, output_tokens=1)


def _assert_counters_match_rollup(agent, calls):
    from agent.turn_finalizer import _rollup_turn_usage

    rollup = _rollup_turn_usage(calls)
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "total_tokens"):
        assert rollup[key] == getattr(agent, f"session_{key}"), key
    for c in calls:  # each row is internally consistent: prompt/total are its own buckets
        assert c["prompt_tokens"] == c["input_tokens"] + c["cache_read_tokens"] + c["cache_write_tokens"]
        assert c["total_tokens"] == c["prompt_tokens"] + c["output_tokens"]
    return rollup


def test_turn_call_rows_carry_billed_prompt_and_total(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    try:
        calls: list = []
        _record(a, _response(_DEFERRED_USAGE), 1, calls)
        _record(a, _response(_CARRYING_USAGE), 2, calls)
        rollup = _assert_counters_match_rollup(a, calls)
        assert rollup["total_tokens"] == sum(BILLED.values()) + OUTPUT == 429_298
        assert a.context_compressor.last_prompt_tokens == sum(LAST.values())
    finally:
        a.close()


@pytest.mark.parametrize("deferred_first", [False, True])
def test_moa_turn_bills_relay_cumulative_aggregator_plus_advisors(tmp_path, monkeypatch, deferred_first):
    a = _agent(tmp_path, monkeypatch)
    try:
        calls: list = []
        if deferred_first:  # advisors that ran on the deferred call still bill; the aggregator bills 0
            a.client = _MoaClient(REF)
            _record(a, _response(_DEFERRED_USAGE), 1, calls)
            assert a.session_input_tokens == REF.input_tokens
            assert a.session_cache_read_tokens == 0
        a.client = _MoaClient(None if deferred_first else REF)
        _record(a, _response(_CARRYING_USAGE), 2, calls)
        assert a.session_input_tokens == BILLED["input_tokens"] + REF.input_tokens
        assert a.session_cache_read_tokens == BILLED["cache_read_tokens"]
        assert a.session_cache_write_tokens == BILLED["cache_write_tokens"]
        assert a.session_output_tokens == OUTPUT + REF.output_tokens
        rollup = _assert_counters_match_rollup(a, calls)
        assert rollup["total_tokens"] == 429_298 + REF.total_tokens
        assert a.context_compressor.last_prompt_tokens == sum(LAST.values())
    finally:
        a.close()


def test_finalizer_window_split_is_the_last_request_not_the_bill(tmp_path, monkeypatch):
    """/context's last-call split decomposes context_used, never the multi-request bill."""
    from agent import turn_finalizer as tf

    a = _agent(tmp_path, monkeypatch)
    captured = {}

    def invoke_hook(name, **kwargs):
        if name == "on_session_end":
            captured["turn_usage"] = kwargs.get("turn_usage")
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)
    try:
        calls: list = []
        _record(a, _response(_DEFERRED_USAGE), 1, calls)
        _record(a, _response(_CARRYING_USAGE), 2, calls)
        tf.emit_session_end(
            a, turn_id="turn", effective_task_id="task", completed=True, failed=False,
            interrupted=False, turn_exit_reason="text_response(stop)",
            original_user_message="go", final_response="ok", turn_calls=calls,
        )
    finally:
        a.close()
    tu = captured["turn_usage"]
    split = tu["last_uncached_tokens"] + tu["last_cache_read_tokens"] + tu["last_cache_write_tokens"]
    assert tu["context_used"] == sum(LAST.values()) == 214_822
    assert split == tu["context_used"]
    # The bill is still the cumulative sum.
    assert tu["cache_read_tokens"] == BILLED["cache_read_tokens"]


class _SlottedMoaClient(_MoaClient):
    """A real-shaped MoA client: resolved aggregator slot plus physical advisor calls."""

    last_aggregator_slot = {"provider": "claude-dtlr", "model": "claude-opus-5-5"}

    def consume_reference_pricing_calls(self):
        return [{"model": "grok-4.7", "provider": "xai-oauth", "base_url": None,
                 "input_tokens": 10, "output_tokens": 0, "cache_read_tokens": 0,
                 "cache_write_tokens": 0, "reasoning_tokens": 0, "http_status": 200}]


def test_moa_physical_calls_and_ledger_parent_carry_the_relay_bill(tmp_path, monkeypatch):
    """Blackbox prices the physical calls and sums them into the composite parent row."""
    from agent import chat_completion_helpers as cch
    from plugins import blackbox
    from plugins.blackbox import store

    a = _agent(tmp_path, monkeypatch)
    store._connect().close()
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650, "prefix_guard": False,
    })
    usage = {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
             "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0},
             "upstream_requests": 2, "cumulative_prompt_tokens": 200,
             "cumulative_prompt_tokens_details": {"input_tokens": 200, "cached_tokens": 0,
                                                  "cache_creation_tokens": 0}}
    resp = _response(usage)
    turn_id = "t:moa-relay"
    try:
        a._current_turn_id = turn_id
        a.client = _SlottedMoaClient(CanonicalUsage(input_tokens=10))
        cch._record_successful_api_call(a, resp)  # the composite's virtual parent row
        calls: list = []
        _record(a, resp, 1, calls)
    finally:
        a.close()
    pricing = calls[-1]["pricing_calls"]
    assert sum(c["input_tokens"] for c in pricing) == 210
    assert pricing[-1]["input_tokens"] == 200  # the aggregator's own physical row is the bill
    assert a.session_input_tokens == 210
    with sqlite3.connect(store._db_path()) as conn:
        parent = conn.execute(
            "SELECT input_tokens FROM turn_api_calls WHERE turn_id=? AND parent_call_id IS NULL",
            (turn_id,)).fetchone()
    assert parent == (210,)


def test_blackbox_rows_store_billed_columns_and_mark_the_deferred_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins import blackbox
    from plugins.blackbox import store

    store._connect().close()
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True, "retention_days": 3650,
    })
    for seq, usage in enumerate((_DEFERRED_USAGE, _CARRYING_USAGE)):
        blackbox.record_api_call(
            turn_id="t-arrival", seq=seq, ts=100.0 + seq, provider="claude-dtlr",
            model="claude-opus-5-5", usage=_response(usage).usage, api_mode="chat_completions",
            sub_key=None, attribution="inferred", http_status=200, relay_synthetic=False,
            route_id=None,
        )
    with sqlite3.connect(store._db_path()) as conn:
        rows = conn.execute(
            "SELECT input_tokens, cache_read, cache_write, output_tokens, usage_deferred, "
            "upstream_requests FROM turn_api_calls WHERE turn_id='t-arrival' ORDER BY seq").fetchall()
    assert rows[0] == (0, 0, 0, 0, 1, None)
    assert rows[1] == (BILLED["input_tokens"], BILLED["cache_read_tokens"],
                       BILLED["cache_write_tokens"], OUTPUT, None, 2)
