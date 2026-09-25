"""T1 core: per-turn token accumulator + on_session_end turn_usage enrichment.

These tests assert the Blackbox core enrichment split across:
- agent/conversation_loop.py: local _turn_calls accumulator populated per successful API call
- agent/turn_finalizer.py: fold _turn_calls into the turn_usage kwarg passed to on_session_end
without leaking into agent global state.
"""
import inspect
import agent.conversation_loop as cl
import agent.turn_finalizer as tf


def test_turn_calls_initialized_local_not_agent_attr():
    """_turn_calls must be a LOCAL var in run_conversation, never agent._turn."""
    src = inspect.getsource(cl.run_conversation)
    assert "_turn_calls: List[Dict[str, Any]] = []" in src, "local accumulator not initialized"
    assert "agent._turn_calls" not in src, "accumulator must not be an agent attribute (re-entrancy)"


def test_append_inside_success_block_only(tmp_path):
    """The append must sit with the session_*_tokens commit, not in retry/except.

    TEST-REPIN (r6 round-4). This asserted `0 < (i_append - i_commit) < 3000`
    over `inspect.getsource(run_conversation)` — a CHARACTER-DISTANCE measure of
    the source text. It is a change-detector twice over: it fails on any comment
    or code added between the two statements with identical runtime behaviour
    (this round's finding-4/7 latch and finding-13 merge pushed it from 2359 to
    4116 and turned it red), and it passes whenever the two statements happen to
    sit close together even if the append is in the WRONG block. `AGENTS.md`
    bans reading source in tests for exactly this pair of failure modes.

    The BEHAVIOUR the name claims is: a successful call contributes to both the
    cumulative counters and the per-turn accumulator, and a FAILED call
    contributes to neither. That is what is asserted here, by driving the real
    `run_conversation` twice — once succeeding, once raising — and reading the
    `turn_usage` the accumulator folds into `on_session_end`.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    import hermes_cli.lifecycle as lifecycle
    from hermes_state import SessionDB
    from run_agent import AIAgent

    seen = {}

    def _fake_invoke_hook(name, **kwargs):
        if name == "on_session_end":
            seen["turn_usage"] = kwargs.get("turn_usage")
        return []

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch.object(lifecycle, "invoke_hook", _fake_invoke_hook),
        ):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_db=db,
                session_id="accumulator-session",
                platform="telegram",
            )
            agent.model = "gpt-4o"
            agent.provider = "openai"
            agent.base_url = None
            agent.client = MagicMock()
            agent.client.chat.completions.create.return_value = SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                model="test/model",
                usage=SimpleNamespace(
                    prompt_tokens=100, completion_tokens=10, total_tokens=110
                ),
            )

            agent.run_conversation("turn one")

            # SUCCESS: both sinks saw exactly this one call.
            assert agent.session_api_calls == 1
            assert seen["turn_usage"]["api_calls"] == 1
            assert seen["turn_usage"]["input_tokens"] == 100
            assert seen["turn_usage"]["output_tokens"] == 10

            # FAILURE: neither sink moves. A call that raised never committed to
            # the cumulative counters, so it must not be in the accumulator
            # either — the two have to agree on what "a call happened" means.
            seen.clear()
            agent.client.chat.completions.create.side_effect = RuntimeError("boom")
            agent.run_conversation("turn two")

            assert agent.session_api_calls == 1, "a failed call must not be counted"
            assert (seen.get("turn_usage") or {}).get("api_calls", 0) == 0, (
                "a failed call must not reach the per-turn accumulator either"
            )
    finally:
        db.close()


def test_on_session_end_carries_turn_usage_kwarg():
    # finalize_turn and the early-exit backstop share one emitter.
    assert "emit_session_end(" in inspect.getsource(tf.finalize_turn)
    assert "emit_session_end(" in inspect.getsource(tf.emit_unfinalized_session_end)
    src = inspect.getsource(tf.emit_session_end)
    assert '"on_session_end",' in src
    # turn_usage kwarg present in the fire call
    fire = src[src.index('"on_session_end",'):]
    assert "turn_usage=" in fire, "on_session_end not enriched with turn_usage"
    assert "user_message=original_user_message" in fire
    assert "final_response=final_response" in fire


def test_turn_usage_fold_sums_calls():
    """Simulate the fold logic on a 2-call turn → summed totals."""
    _turn_calls = [
        {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 80,
         "cache_write_tokens": 5, "reasoning_tokens": 2, "total_tokens": 110, "latency_s": 1.5},
        {"input_tokens": 200, "output_tokens": 20, "cache_read_tokens": 150,
         "cache_write_tokens": 0, "reasoning_tokens": 3, "total_tokens": 220, "latency_s": 2.0},
    ]
    summary = {
        "api_calls": len(_turn_calls),
        "input_tokens": sum(c["input_tokens"] for c in _turn_calls),
        "output_tokens": sum(c["output_tokens"] for c in _turn_calls),
        "cache_read_tokens": sum(c["cache_read_tokens"] for c in _turn_calls),
        "total_tokens": sum(c["total_tokens"] for c in _turn_calls),
        "latency_s": sum(c["latency_s"] for c in _turn_calls),
    }
    assert summary["api_calls"] == 2
    assert summary["input_tokens"] == 300
    assert summary["output_tokens"] == 30
    assert summary["cache_read_tokens"] == 230
    assert summary["total_tokens"] == 330
    assert summary["latency_s"] == 3.5


def test_turn_usage_includes_last_call_split_keys():
    """The fold must surface the FINAL call's cache split (window decomposition)."""
    src = inspect.getsource(tf.emit_session_end)
    fire = src[src.index('"on_session_end",'):]
    # The payload assembled just above the fire carries the three last-call keys.
    block = src[:src.index('"on_session_end",')]
    assert '"last_cache_read_tokens": _last_cache_read' in block
    assert '"last_cache_write_tokens": _last_cache_write' in block
    assert '"last_uncached_tokens": _last_uncached' in block
    # Sourced from the LAST call, not summed.
    assert "_last_call = _turn_calls[-1]" in block


def test_last_call_split_taken_from_final_call_not_summed():
    """Simulate the fold's last-call extraction on a 3-call turn."""
    _turn_calls = [
        {"input_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 100,
         "output_tokens": 5, "reasoning_tokens": 0, "total_tokens": 107, "latency_s": 1.0},
        {"input_tokens": 2, "cache_read_tokens": 100, "cache_write_tokens": 50,
         "output_tokens": 5, "reasoning_tokens": 0, "total_tokens": 157, "latency_s": 1.0},
        {"input_tokens": 2, "cache_read_tokens": 150, "cache_write_tokens": 30,
         "output_tokens": 5, "reasoning_tokens": 0, "total_tokens": 187, "latency_s": 1.0},
    ]
    _last_call = _turn_calls[-1]
    last_cache_read = int(_last_call.get("cache_read_tokens", 0) or 0)
    last_cache_write = int(_last_call.get("cache_write_tokens", 0) or 0)
    last_uncached = int(_last_call.get("input_tokens", 0) or 0)
    # Final call only — NOT the summed 250 cache_read across all calls.
    assert last_cache_read == 150
    assert last_cache_write == 30
    assert last_uncached == 2
    # The three sum to the final call's prompt_tokens (== context_used invariant).
    assert last_cache_read + last_cache_write + last_uncached == 182
