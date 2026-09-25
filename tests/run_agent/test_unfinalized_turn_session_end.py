"""Every turn fires ``on_session_end`` — including turns that end early.

``run_conversation`` has many early ``return``s (fallback chain exhausted,
interrupt unwind, truncated tool args, ...) and can raise; none reach
``finalize_turn``. Before t_6c09f0c2 those turns never fired the
once-per-turn ``on_session_end`` hook, so Blackbox wrote their
``turn_api_calls`` rows with no parent ``turns`` row (262/262 turns ending on
a trailing provider error were parentless vs 13% of clean ones). The
``AIAgent.run_conversation`` forwarder now backstops it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from agent.turn_finalizer import emit_unfinalized_session_end


def _make_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        return agent


class _Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, hook_name, **kwargs):
        if hook_name == "on_session_end":
            self.calls.append(kwargs)
        return []


def _run(agent, fake_loop):
    cap = _Capture()
    with (
        patch("hermes_cli.lifecycle.invoke_hook", cap),
        patch("agent.conversation_loop.run_conversation", fake_loop),
    ):
        try:
            result = agent.run_conversation("hello")
        except BaseException as exc:  # noqa: BLE001 - re-raised by forwarder
            result = exc
    return cap, result


def _start_turn(agent):
    """What turn_context + run_conversation's prologue publish for a turn."""
    turn_id = agent._relay_pending_turn_id
    agent._current_turn_id = turn_id
    agent._current_task_id = "task"
    calls = [{"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0,
              "cache_write_tokens": 0, "reasoning_tokens": 0, "total_tokens": 12}]
    agent._blackbox_turn_calls = (turn_id, calls)
    agent._turn_original_user_message = (turn_id, "hello")
    return turn_id


def test_failed_early_return_fires_session_end_once():
    agent = _make_agent()
    seen = {}

    def fake_loop(ag, *a, **k):
        seen["turn_id"] = _start_turn(ag)
        return {"final_response": "API call failed after 3 retries: HTTP 503",
                "messages": [], "api_calls": 1, "completed": False,
                "failed": True, "error": "HTTP 503: pool exhausted\ndetail"}

    cap, result = _run(agent, fake_loop)
    assert isinstance(result, dict) and result["failed"] is True
    assert len(cap.calls) == 1
    ev = cap.calls[0]
    assert ev["turn_id"] == seen["turn_id"]
    assert ev["failed"] is True and ev["completed"] is False
    assert ev["interrupted"] is False
    assert ev["final_response"] == ""
    assert ev["turn_exit_reason"] == "early_return:HTTP 503: pool exhausted"
    assert ev["user_message"] == "hello"
    assert ev["turn_usage"]["api_calls"] == 1
    assert ev["turn_usage"]["input_tokens"] == 10


def test_raise_fires_session_end_and_reraises():
    agent = _make_agent()

    def fake_loop(ag, *a, **k):
        _start_turn(ag)
        raise RuntimeError("fallback chain exhausted")

    cap, result = _run(agent, fake_loop)
    assert isinstance(result, RuntimeError)
    assert len(cap.calls) == 1
    assert cap.calls[0]["failed"] is True
    assert cap.calls[0]["turn_exit_reason"] == "exception:RuntimeError"
    assert cap.calls[0]["final_response"] == ""


def test_interrupted_early_return_is_not_marked_failed():
    agent = _make_agent()

    def fake_loop(ag, *a, **k):
        _start_turn(ag)
        return {"final_response": "stopped", "messages": [], "api_calls": 0,
                "completed": False, "interrupted": True}

    cap, _ = _run(agent, fake_loop)
    assert len(cap.calls) == 1
    assert cap.calls[0]["interrupted"] is True
    assert cap.calls[0]["failed"] is False


def test_finalized_turn_is_not_emitted_twice():
    agent = _make_agent()

    def fake_loop(ag, *a, **k):
        turn_id = _start_turn(ag)
        ag._session_end_emitted_turn_id = turn_id  # finalize_turn ran
        return {"final_response": "ok", "messages": [], "api_calls": 1,
                "completed": True}

    cap, _ = _run(agent, fake_loop)
    assert cap.calls == []


def test_turn_that_never_started_is_not_emitted():
    agent = _make_agent()
    agent._current_turn_id = "previous:turn:deadbeef"

    def fake_loop(ag, *a, **k):
        raise RuntimeError("before turn setup")

    cap, _ = _run(agent, fake_loop)
    assert cap.calls == []


def test_stale_published_calls_are_not_attributed():
    agent = _make_agent()
    agent._current_turn_id = "s:t:new"
    agent._blackbox_turn_calls = ("s:t:old", [{"input_tokens": 99}])
    agent._turn_original_user_message = ("s:t:old", "old message")
    cap = _Capture()
    with patch("hermes_cli.lifecycle.invoke_hook", cap):
        assert emit_unfinalized_session_end(agent, "s:t:new", exc=RuntimeError()) is True
        assert emit_unfinalized_session_end(agent, "s:t:new", exc=RuntimeError()) is False
    assert len(cap.calls) == 1
    assert "input_tokens" not in (cap.calls[0]["turn_usage"] or {})
    assert cap.calls[0]["user_message"] is None


def test_backstop_never_raises():
    agent = _make_agent()
    agent._current_turn_id = "s:t:x"

    def boom(*a, **k):
        raise RuntimeError("plugin exploded")

    with patch("hermes_cli.lifecycle.invoke_hook", boom):
        # emit_session_end swallows hook failures; the backstop still reports
        # that it attempted the emission and never raises into the turn.
        assert emit_unfinalized_session_end(agent, "s:t:x", result={"failed": True}) is True


def test_real_loop_non_retryable_error_fires_session_end():
    """Drive the REAL conversation loop to its non-retryable-error early return."""
    import httpx
    import openai

    agent = _make_agent()
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    body = {"error": {"message": "bad request probe"}}
    err = openai.BadRequestError(
        "bad request probe", response=httpx.Response(400, request=req, json=body), body=body
    )
    agent.client.chat.completions.create.side_effect = err
    agent._interruptible_api_call = MagicMock(side_effect=err)
    agent._interruptible_streaming_api_call = MagicMock(side_effect=err)
    cap = _Capture()
    with patch("hermes_cli.lifecycle.invoke_hook", cap), patch("time.sleep"):
        result = agent.run_conversation("hello")
    assert result["failed"] is True and result["completed"] is False
    assert len(cap.calls) == 1
    ev = cap.calls[0]
    assert ev["turn_id"] == agent._current_turn_id
    assert ev["failed"] is True and ev["final_response"] == ""
    assert ev["turn_exit_reason"].startswith("early_return:HTTP 400")
