"""Full-loop regression: a successful response with NO usage payload is still accounted.

FleetReview r3 finding 5 (`Missing Usage`). `_canonical_usage_from_response`
already turns an omitted usage payload into an aggregate UNKNOWN, but the
per-call commit block was gated on `response.usage` being truthy, so the
UNKNOWN never reached session counters, `last_turn_usage`, the Blackbox
`_turn_calls` accumulator, pricing, or persistence — the call was reported as
if it had never happened.

These pins drive the real `AIAgent.run_conversation()` loop (a helper-level
test cannot see the drop, because the helper already returned the right value).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _response(*, usage=None, content="successful response"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    kwargs = {"choices": [choice], "model": "test/model"}
    if usage is not None:
        kwargs["usage"] = SimpleNamespace(**usage)
    return SimpleNamespace(**kwargs)


def _make_agent(session_db, response):
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
            session_db=session_db,
            session_id="usageless-session",
            platform="telegram",
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = response
    # Pin a route that HAS a pricing entry, so "unpriceable" can only come from
    # the usage being unknown rather than from a missing catalog entry.
    agent.model = "gpt-4o"
    agent.provider = "openai"
    agent.base_url = None
    return agent


@pytest.fixture
def captured_turn_usage(monkeypatch):
    """Capture the ``turn_usage`` kwarg the on_session_end hook receives."""
    import hermes_cli.lifecycle as lifecycle

    seen = {}

    def _fake_invoke_hook(name, **kwargs):
        if name == "on_session_end":
            seen["turn_usage"] = kwargs.get("turn_usage")

    monkeypatch.setattr(lifecycle, "invoke_hook", _fake_invoke_hook)
    return seen


def test_usageless_success_reaches_the_real_accounting_path(captured_turn_usage):
    """The whole commit block runs, carrying aggregate UNKNOWN rather than nothing."""
    session_db = MagicMock()
    agent = _make_agent(session_db, _response(usage=None))

    result = agent.run_conversation("hello")

    assert result["final_response"] == "successful response"
    # The call happened and is counted — not silently dropped.
    assert agent.session_api_calls == 1
    # Live last-turn snapshot exists and declares itself unmeasured.
    assert agent.last_turn_usage is not None
    assert agent.last_turn_usage["usage_unknown"] is True
    # Pricing refused rather than inventing a measured $0 spend.
    assert agent.session_cost_status == "unknown"
    assert agent.session_estimated_cost_usd == 0
    # Persistence was queued for the call.
    session_db.queue_token_counts.assert_called_once()
    # Blackbox per-turn accumulator saw the physical call, flagged unknown.
    turn_usage = captured_turn_usage["turn_usage"]
    assert turn_usage is not None
    assert turn_usage["api_calls"] == 1
    assert turn_usage["usage_unknown"] is True
    assert len(turn_usage["calls"]) == 1
    assert turn_usage["calls"][0]["usage_unknown"] is True


def test_measured_response_control_is_unaffected(captured_turn_usage):
    """Control: a normal measured response still accounts exactly as before."""
    session_db = MagicMock()
    agent = _make_agent(
        session_db,
        _response(usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}),
    )

    agent.run_conversation("hello")

    assert agent.session_api_calls == 1
    assert agent.session_total_tokens == 18
    assert agent.last_turn_usage["usage_unknown"] is False
    assert agent.session_cost_status != "unknown"
    session_db.queue_token_counts.assert_called_once()
    turn_usage = captured_turn_usage["turn_usage"]
    assert turn_usage["usage_unknown"] is False
    assert turn_usage["output_tokens"] == 7


def test_usageless_turn_renders_unknown_not_a_measured_zero():
    """The shipped /usage thin renderer must refuse to present the zeros."""
    from gateway.slash_commands import render_thin_last_turn_lines

    session_db = MagicMock()
    agent = _make_agent(session_db, _response(usage=None))
    agent.run_conversation("hello")

    text = "\n".join(render_thin_last_turn_lines(agent.last_turn_usage, "resident"))
    assert "Total (billed in+out): unknown" in text
    assert "Total (billed in+out): 0" not in text


def test_no_call_normalization_still_means_a_known_zero():
    """Settled contract control: ``normalize_usage(None)`` is not an unknown."""
    from agent.usage_pricing import normalize_usage

    no_call = normalize_usage(None)
    assert no_call.usage_unknown is False
    assert no_call.total_tokens_unknown is False
    assert no_call.total_tokens == 0
