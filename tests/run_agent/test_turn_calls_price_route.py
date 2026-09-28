"""Full-loop regression: every Blackbox per-turn call carries the route that SERVED it.

t_0c5c3822 (subs.ace S7 §7): the main-loop ``_turn_calls`` entry carried tokens
but no ``model``/``provider``/``base_url``, so ``plugins.blackbox.cost.
compute_turn_cost`` priced every call of a turn at the turn's FINAL route. A turn
that changed model mid-turn (fallback, /model) was priced at the wrong rate:
measured 2026-09-27 over 1.5 d of live stores, 68 mixed-route turns priced
$519.06 vs $597.62 for the same calls at their own routes (single-route turns:
$0.40 apart on $5,597). ``turn_api_calls`` already records the per-call route,
so ``turns.cost_usd`` disagreed with the ledger built from those rows.

This drives the real ``AIAgent.run_conversation()`` over a two-call turn whose
route changes between call 1 and call 2, then prices the captured calls with the
real ``compute_turn_cost``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
from plugins.blackbox.cost import compute_turn_cost
from run_agent import AIAgent

_USAGE = {"prompt_tokens": 40_000, "completion_tokens": 2_000, "total_tokens": 42_000}


def _response(*, tool_calls=None, content="done"):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls" if tool_calls else "stop")
    return SimpleNamespace(choices=[choice], model="x", usage=SimpleNamespace(**_USAGE))


def _tool_call():
    return SimpleNamespace(
        id="call_1", type="function",
        function=SimpleNamespace(name="no_such_tool", arguments="{}"),
    )


@pytest.fixture
def captured_turn_usage(monkeypatch):
    import hermes_cli.lifecycle as lifecycle

    seen = {}

    def _fake_invoke_hook(name, **kwargs):
        if name == "on_session_end":
            seen["turn_usage"] = kwargs.get("turn_usage")
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", _fake_invoke_hook)
    return seen


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
            session_id="route-switch-session",
            platform="telegram",
        )
    agent.client = MagicMock()
    agent.model = "gpt-4o"
    agent.provider = "openai"
    agent.base_url = None
    return agent


def _price(model, provider, call):
    usage = CanonicalUsage(
        input_tokens=call["input_tokens"], output_tokens=call["output_tokens"],
        cache_read_tokens=call["cache_read_tokens"], cache_write_tokens=call["cache_write_tokens"],
        reasoning_tokens=call["reasoning_tokens"],
    )
    return float(estimate_usage_cost(model, usage, provider=provider, base_url=None).amount_usd)


def test_turn_calls_carry_the_route_that_served_each_call(captured_turn_usage):
    agent = _make_agent()
    responses = iter([_response(tool_calls=[_tool_call()], content=None), _response()])

    def _create(*args, **kwargs):
        resp = next(responses)
        if agent.session_api_calls == 0:
            # Mid-turn route change (fallback / model switch) landing while
            # call 1 is in flight: call 1 was sent on gpt-4o, call 2 is built
            # and sent on gpt-4o-mini.
            agent.model = "gpt-4o-mini"
        return resp

    agent.client.chat.completions.create.side_effect = _create
    agent.run_conversation("hello")

    calls = captured_turn_usage["turn_usage"]["calls"]
    assert [c.get("model") for c in calls] == ["gpt-4o", "gpt-4o-mini"]
    # The route recorded for each call is the route its REQUEST was sent on.
    requested = [
        c.kwargs.get("model") for c in agent.client.chat.completions.create.call_args_list
    ]
    assert [c.get("model") for c in calls] == requested
    assert [c.get("provider") for c in calls] == ["openai", "openai"]
    assert all("base_url" in c for c in calls)

    # The turn is recorded at its FINAL route; each call must still price at its own.
    total, status, _ = compute_turn_cost("gpt-4o-mini", "openai", None, calls)
    expected = _price("gpt-4o", "openai", calls[0]) + _price("gpt-4o-mini", "openai", calls[1])
    wrong = _price("gpt-4o-mini", "openai", calls[0]) + _price("gpt-4o-mini", "openai", calls[1])
    assert status != "unknown"
    assert expected != pytest.approx(wrong)
    assert total == pytest.approx(expected)


def test_route_change_during_the_call_does_not_restamp_that_call(captured_turn_usage, monkeypatch):
    """FleetReview 65e315f38776: a route change WHILE a request is in flight
    (``/model`` or fallback from another thread) must not re-stamp the call that
    was already sent. Every post-call reader -- the Blackbox ``_turn_calls``
    entry, the session cost estimate, the state.db delta and the chokepoint
    ``turn_api_calls`` row -- must price at the route the request went out on.
    """
    import plugins.blackbox as blackbox

    ledger = []
    monkeypatch.setattr(
        blackbox, "record_api_call",
        lambda **kw: ledger.append((kw.get("provider"), kw.get("model"))),
    )
    agent = _make_agent()
    responses = iter([_response(tool_calls=[_tool_call()], content=None), _response()])

    def _create(*args, **kwargs):
        # Switch route mid-flight on EVERY call: the request carrying
        # kwargs["model"] is already on the wire.
        agent.model = "gpt-4o-mini" if kwargs.get("model") == "gpt-4o" else "gpt-4o"
        agent.provider = "openrouter"
        return next(responses)

    agent.client.chat.completions.create.side_effect = _create
    agent.run_conversation("hello")

    requested = [
        c.kwargs.get("model") for c in agent.client.chat.completions.create.call_args_list
    ]
    assert requested == ["gpt-4o", "gpt-4o-mini"]
    calls = captured_turn_usage["turn_usage"]["calls"]
    assert [c.get("model") for c in calls] == requested
    assert [c.get("provider") for c in calls] == ["openai", "openrouter"]
    assert [m for _p, m in ledger] == requested
    assert [p for p, _m in ledger] == ["openai", "openrouter"]

    # Session cost is the sum of each call at its OWN request route.
    expected = _price("gpt-4o", "openai", calls[0]) + _price("gpt-4o-mini", "openrouter", calls[1])
    assert agent.session_estimated_cost_usd == pytest.approx(expected)
