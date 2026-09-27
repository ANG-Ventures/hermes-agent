"""Blackbox compaction attribution on the REAL LCM engine (card t_7b64083c F2).

Every live profile runs ``context.engine: lcm``. LCMEngine never fills the
builtin compressor's ``_last_compression_telemetry``, so the first version of
the Blackbox columns recorded ``compaction_cost_usd = NULL`` fleet-wide. The
cost is now captured engine-agnostically: ``compress_context`` opens an aux
cost sink around ``compress`` and every ``call_llm`` inside it prices its
observed usage.

This arm drives the real path end to end: AIAgent._compress_context -> real
LCMEngine.compress -> real escalation summarizer -> real ``call_llm``. Only the
provider transport (``_call_llm_impl``) is replaced, with a response carrying
measured usage, so nothing touches the network.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine
from plugins.context_engine.lcm.tokens import count_messages_tokens

SUMMARY_MODEL = "claude-haiku-4-5"


def _agent(tmp_path: Path, engine: LCMEngine):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("BB_LCM", source="discord")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1",
            model=SUMMARY_MODEL, quiet_mode=True, session_db=db,
            session_id="BB_LCM", skip_context_files=True, skip_memory=True,
        )
    agent.context_compressor = engine
    # What build_turn_context installs at the start of every turn.
    agent._blackbox_compaction = {"idle_compaction_fired": False}
    return agent


def _engine(tmp_path: Path) -> LCMEngine:
    engine = LCMEngine(config=LCMConfig(
        database_path=str(tmp_path / "lcm.db"), fresh_tail_count=2,
        leaf_chunk_tokens=50, context_threshold=0.01,
    ), hermes_home=str(tmp_path))
    engine.update_model(SUMMARY_MODEL, 200_000, provider="anthropic")
    engine.on_session_start("BB_LCM", hermes_home=str(tmp_path), model=SUMMARY_MODEL,
                            provider="anthropic", context_length=200_000, platform="pytest")
    return engine


def _messages() -> list:
    blob = "context payload sentence number " * 60
    msgs = [{"role": "system", "content": "You are testing LCM."}]
    for i in range(12):
        msgs.append({"role": "user", "content": f"user turn {i} {blob}"})
        msgs.append({"role": "assistant", "content": f"assistant turn {i} {blob}"})
    return msgs


def _transport(calls: list, *, usage: bool = True):
    def fake_impl(**kwargs):
        route = kwargs.get("route_info")
        if isinstance(route, dict):
            route["provider"] = "anthropic"
            route["model"] = SUMMARY_MODEL
        calls.append(kwargs.get("task"))
        return SimpleNamespace(
            model=SUMMARY_MODEL,
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="Compacted earlier turns: kept the key facts."))],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100,
                                  total_tokens=1100) if usage else None,
        )
    return fake_impl


def _expected_cost(n_calls: int) -> float:
    from agent.usage_pricing import estimate_usage_cost, normalize_usage

    # Independent oracle: the canonical pricer on the chat-completions reading
    # of the same usage the transport returned.
    one = estimate_usage_cost(SUMMARY_MODEL, normalize_usage(
        SimpleNamespace(prompt_tokens=1000, completion_tokens=100, total_tokens=1100),
        api_mode="chat_completions"), provider="anthropic").amount_usd
    assert one is not None and float(one) > 0, "oracle must price the summary model"
    return round(float(one) * n_calls, 12)


def test_lcm_committed_compaction_records_before_after_and_cost(tmp_path, monkeypatch):
    import agent.auxiliary_client as aux

    calls: list = []
    monkeypatch.setattr(aux, "_call_llm_impl", _transport(calls))
    engine = _engine(tmp_path)
    agent = _agent(tmp_path, engine)
    assert not hasattr(engine, "_last_compression_telemetry"), (
        "premise: LCM does not expose the builtin telemetry the old path read")

    from agent.model_metadata import estimate_request_tokens_rough

    messages = _messages()
    approx = count_messages_tokens(messages)
    # before/after must share ONE basis (request-level: messages+system+tools);
    # the caller's messages-only ``approx`` would make after > before.
    request_before = estimate_request_tokens_rough(
        messages, system_prompt="You are testing LCM.", tools=agent.tools or None)
    assert request_before > approx
    agent._compress_context(messages, "You are testing LCM.", approx_tokens=approx,
                            trigger_reason="threshold")

    assert engine._last_compression_status == "compacted"
    assert calls, "the real LCM summarizer must have reached call_llm"
    state = agent._blackbox_compaction
    # Request-level basis (the compaction's tool refresh may add the lcm_*
    # schemas before the estimate, so it can only be >= the pre-refresh figure).
    assert state["compaction_tokens_before"] >= request_before
    assert isinstance(state["compaction_tokens_after"], int)
    assert 0 < state["compaction_tokens_after"] < state["compaction_tokens_before"]
    assert state["compaction_cost_usd"] == _expected_cost(len(calls))
    assert state["idle_compaction_fired"] is False


def test_lcm_unpriced_summary_call_keeps_cost_null(tmp_path, monkeypatch):
    import agent.auxiliary_client as aux

    calls: list = []
    monkeypatch.setattr(aux, "_call_llm_impl", _transport(calls, usage=False))
    engine = _engine(tmp_path)
    agent = _agent(tmp_path, engine)
    messages = _messages()
    agent._compress_context(messages, "You are testing LCM.",
                            approx_tokens=count_messages_tokens(messages))
    assert engine._last_compression_status == "compacted" and calls
    assert agent._blackbox_compaction["compaction_cost_usd"] is None
    assert agent._blackbox_compaction["compaction_tokens_before"] is not None


def test_lcm_noop_compaction_after_priced_one_keeps_turn_priced(tmp_path, monkeypatch, caplog):
    """t_6583c046: live turns ran a real compaction, then pre-API pressure
    re-fired and LCM returned the transcript unchanged (N->N, zero summarizer
    calls). The zero-call sink fell through to LCM's absent telemetry and
    poisoned the whole turn to NULL (16/16 NULL turns on 09-24/25). A no-op
    spent nothing: the turn keeps the first compaction's exact price."""
    import agent.auxiliary_client as aux

    calls: list = []
    monkeypatch.setattr(aux, "_call_llm_impl", _transport(calls))
    engine = _engine(tmp_path)
    agent = _agent(tmp_path, engine)
    messages = _messages()
    compressed, _ = agent._compress_context(
        messages, "You are testing LCM.",
        approx_tokens=count_messages_tokens(messages), trigger_reason="threshold")
    assert calls and engine._last_compression_status == "compacted"
    priced = agent._blackbox_compaction["compaction_cost_usd"]
    assert priced == _expected_cost(len(calls))

    import agent.conversation_compression as cc
    recorded: list = []
    real_record = cc._record_blackbox_compaction

    def spy(*a, **kw):
        recorded.append(kw)
        return real_record(*a, **kw)

    monkeypatch.setattr(cc, "_record_blackbox_compaction", spy)
    n_calls = len(calls)
    out, _ = agent._compress_context(list(compressed), "You are testing LCM.",
                                     approx_tokens=count_messages_tokens(compressed),
                                     trigger_reason="pre_api_pressure")
    assert len(calls) == n_calls, "premise: the second compaction made no summarizer call"
    assert len(out) == len(compressed), "premise: the second compaction was a no-op"
    assert len(recorded) == 1 and recorded[0]["noop"] is True, (
        "premise: the no-op was committed and recorded")
    assert agent._blackbox_compaction["compaction_cost_usd"] == priced


def test_zero_call_non_noop_compaction_stays_unknown_and_logs_reason(caplog):
    """A compaction that CHANGED the transcript without any observed call_llm
    is not provably free: fail closed and say why."""
    import logging
    from agent.conversation_compression import _record_blackbox_compaction

    agent = SimpleNamespace(_blackbox_compaction={"idle_compaction_fired": False})
    with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
        _record_blackbox_compaction(agent, trigger="threshold", before=10, after=5,
                                    telemetry=None,
                                    cost_sink={"usd": 0.0, "calls": 0, "unknown": False})
    assert agent._blackbox_compaction["compaction_cost_usd"] is None
    assert "reason=no_calls_no_telemetry_cost" in caplog.text


def test_unpriced_sink_logs_first_unknown_reason(caplog):
    import logging
    import agent.auxiliary_client as aux
    from agent.conversation_compression import _record_blackbox_compaction

    sink = aux.new_aux_cost_sink()
    with aux.aux_cost_sink(sink):
        aux._record_aux_call_cost(SimpleNamespace(usage=None, model="m"), {}, streamed=False)
        aux._record_aux_call_cost(SimpleNamespace(usage=None, model="m"), {}, streamed=True)
    assert sink["unknown"] is True and sink["unknown_reason"] == "no_usage"
    agent = SimpleNamespace(_blackbox_compaction={"idle_compaction_fired": False})
    with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
        _record_blackbox_compaction(agent, trigger="threshold", before=10, after=5,
                                    telemetry=None, cost_sink=sink, noop=True)
    assert agent._blackbox_compaction["compaction_cost_usd"] is None
    assert "reason=sink_unknown:no_usage" in caplog.text


def test_sink_does_not_leak_outside_compaction(monkeypatch):
    import agent.auxiliary_client as aux

    calls: list = []
    monkeypatch.setattr(aux, "_call_llm_impl", _transport(calls))
    sink = aux.new_aux_cost_sink()
    with aux.aux_cost_sink(sink):
        aux.call_llm(task="compression", messages=[{"role": "user", "content": "x"}])
    aux.call_llm(task="compression", messages=[{"role": "user", "content": "y"}])
    assert sink["calls"] == 1 and aux.aux_cost_sink_total(sink) == _expected_cost(1)


def test_openai_shaped_usage_on_anthropic_route_is_not_priced_as_zero(monkeypatch):
    """Regression: normalizing prompt_tokens as Anthropic reads 0 tokens -> $0.00."""
    import agent.auxiliary_client as aux

    monkeypatch.setattr(aux, "_call_llm_impl", _transport([]))
    sink = aux.new_aux_cost_sink()
    with aux.aux_cost_sink(sink):
        aux.call_llm(task="compression", messages=[{"role": "user", "content": "x"}])
    assert aux.aux_cost_sink_total(sink) > 0
