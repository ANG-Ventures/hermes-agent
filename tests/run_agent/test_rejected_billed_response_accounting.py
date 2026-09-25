"""I4 class 2: a billed HTTP-200 the loop REJECTS must still reach the turn totals.

The per-call ledger (Blackbox ``turn_api_calls``) is written at the transport
chokepoint (``chat_completion_helpers._record_successful_api_call``) for every
HTTP-200. The session counters and the ``_turn_calls`` rollup that becomes the
``turns`` row used to commit only the response the loop ACCEPTED, so a 200 the
loop then classified as a failure (safety refusal, malformed shape) and failed
over from was billed, sat in the ledger, and was missing from the turn totals.

Production evidence (argus store, turn ``20260924_050908_28d3ad:…``): ledger
seq 3 = claude-apr HTTP 200 (out 10606, cache_read 135126) followed by a
failover; ``turns.api_calls`` was the ledger count minus that row.

These pins drive the REAL ``AIAgent.run_conversation()`` through the real
transport chokepoint and the real turn finalizer, and assert the invariant
directly: session counters == turn rollup == sum of the ledger rows.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_loop import _settle_unaccepted_billed_responses
from agent.usage_pricing import normalize_usage
from run_agent import AIAgent

_REJECTED_USAGE = {
    "prompt_tokens": 136484,
    "completion_tokens": 10606,
    "total_tokens": 147090,
}
_ACCEPTED_USAGE = {"prompt_tokens": 5000, "completion_tokens": 200, "total_tokens": 5200}


def _response(*, usage, content="ok", finish_reason="stop", choices=True):
    kwargs = {"model": "test/model", "usage": SimpleNamespace(**usage)}
    if choices:
        msg = SimpleNamespace(content=content, tool_calls=None, refusal=None)
        kwargs["choices"] = [SimpleNamespace(message=msg, finish_reason=finish_reason)]
    else:
        kwargs["choices"] = []
    return SimpleNamespace(**kwargs)


def _make_agent(responses):
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
            platform="telegram",
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = list(responses)
    agent.model = "gpt-4o"
    agent.provider = "openai"
    agent.base_url = None
    return agent


def _install_one_shot_failover(agent):
    """Stand in for a configured fallback: switch route once, then exhaust."""
    state = {"used": False}

    def _fake_fallback(*_args, **_kwargs):
        if state["used"]:
            return False
        state["used"] = True
        agent.model = "gpt-4o-mini"
        return True

    agent._try_activate_fallback = _fake_fallback
    agent._has_pending_fallback = lambda: not state["used"]
    return state


@pytest.fixture
def ledger(monkeypatch):
    """Capture every per-call ledger row the transport chokepoint emits."""
    import plugins.blackbox as blackbox

    rows = []
    monkeypatch.setattr(blackbox, "record_api_call", lambda **kw: rows.append(kw))
    return rows


def _lifecycle_module():
    """The module turn_finalizer imports ``invoke_hook`` from (resolved from its
    source so this pin follows the finalizer if the hook module moves)."""
    import importlib
    import inspect

    from agent import turn_finalizer

    line = next(
        l for l in inspect.getsource(turn_finalizer).splitlines()
        if "import invoke_hook as _invoke_hook" in l
    )
    return importlib.import_module(line.split("from", 1)[1].split("import", 1)[0].strip())


@pytest.fixture
def turn_usage(monkeypatch):
    """Capture the ``turn_usage`` the real finalizer hands to on_session_end."""
    seen = []

    def _fake_invoke_hook(name, **kwargs):
        if name == "on_session_end":
            seen.append(kwargs.get("turn_usage"))
        # Same return type as the real invoke_hook with no plugin handlers.
        return []

    monkeypatch.setattr(_lifecycle_module(), "invoke_hook", _fake_invoke_hook)
    return seen


def _ledger_sums(rows):
    usages = [
        normalize_usage(r["usage"], provider=r["provider"], api_mode=r["api_mode"])
        for r in rows
        if r.get("http_status") == 200
    ]
    return {
        "api_calls": len(usages),
        "input_tokens": sum(u.input_tokens for u in usages),
        "output_tokens": sum(u.output_tokens for u in usages),
        "cache_read_tokens": sum(u.cache_read_tokens for u in usages),
        "cache_write_tokens": sum(u.cache_write_tokens for u in usages),
    }


def _assert_counters_match_ledger(agent, sums):
    assert agent.session_api_calls == sums["api_calls"]
    assert agent.session_input_tokens == sums["input_tokens"]
    assert agent.session_output_tokens == sums["output_tokens"]
    assert agent.session_cache_read_tokens == sums["cache_read_tokens"]
    assert agent.session_cache_write_tokens == sums["cache_write_tokens"]


def _assert_rollup_matches_ledger(rollup, sums):
    assert rollup is not None
    assert rollup["api_calls"] == sums["api_calls"]
    assert rollup["input_tokens"] == sums["input_tokens"]
    assert rollup["output_tokens"] == sums["output_tokens"]
    assert rollup["cache_read_tokens"] == sums["cache_read_tokens"]
    assert rollup["cache_write_tokens"] == sums["cache_write_tokens"]


def test_refused_200_then_failover_is_in_the_turn_totals(ledger, turn_usage):
    """Safety refusal (content_filter) with usage -> failover -> accepted."""
    agent = _make_agent([
        _response(usage=_REJECTED_USAGE, content="", finish_reason="content_filter"),
        _response(usage=_ACCEPTED_USAGE, content="answer"),
    ])
    _install_one_shot_failover(agent)

    result = agent.run_conversation("hello")

    assert result["final_response"] == "answer"
    assert len(ledger) == 2, "both HTTP-200s must reach the per-call ledger"
    sums = _ledger_sums(ledger)
    assert sums["output_tokens"] == 10606 + 200
    _assert_counters_match_ledger(agent, sums)
    _assert_rollup_matches_ledger(turn_usage[-1], sums)
    # The rejected call is priced at the route that SERVED it, not the fallback.
    rejected = [c for c in turn_usage[-1]["calls"] if c.get("accepted") is False]
    assert len(rejected) == 1
    assert rejected[0]["model"] == "gpt-4o"
    assert rejected[0]["output_tokens"] == 10606
    # Chronological: the rejected call precedes the accepted one.
    assert turn_usage[-1]["calls"][-1]["output_tokens"] == 200


def test_malformed_200_then_failover_is_in_the_turn_totals(ledger, turn_usage):
    """Empty ``choices`` with a usage payload -> invalid-response failover."""
    agent = _make_agent([
        _response(usage=_REJECTED_USAGE, choices=False),
        _response(usage=_ACCEPTED_USAGE, content="answer"),
    ])
    _install_one_shot_failover(agent)

    result = agent.run_conversation("hello")

    assert result["final_response"] == "answer"
    sums = _ledger_sums(ledger)
    assert sums["api_calls"] == 2
    _assert_counters_match_ledger(agent, sums)
    _assert_rollup_matches_ledger(turn_usage[-1], sums)


def test_accepted_only_turn_is_not_double_counted(ledger, turn_usage):
    """Control: the ordinary path accounts each billed call exactly once."""
    agent = _make_agent([_response(usage=_ACCEPTED_USAGE, content="answer")])

    agent.run_conversation("hello")

    sums = _ledger_sums(ledger)
    assert sums["api_calls"] == 1
    _assert_counters_match_ledger(agent, sums)
    _assert_rollup_matches_ledger(turn_usage[-1], sums)
    assert not getattr(agent, "_billed_unaccounted", [])


def test_rejected_call_on_an_early_return_turn_reaches_session_totals(ledger, turn_usage):
    """No fallback: the refusal returns early (no finalizer). The spend is
    settled into the SESSION at the next turn, never into that turn's rollup."""
    agent = _make_agent([
        _response(usage=_REJECTED_USAGE, content="", finish_reason="content_filter"),
        _response(usage=_ACCEPTED_USAGE, content="answer"),
    ])
    agent._try_activate_fallback = lambda *a, **k: False
    agent._has_pending_fallback = lambda: False

    agent.run_conversation("turn one")
    agent.run_conversation("turn two")

    sums = _ledger_sums(ledger)
    assert sums["api_calls"] == 2
    _assert_counters_match_ledger(agent, sums)
    # Turn two's rollup holds only turn two's own call.
    assert turn_usage[-1]["api_calls"] == 1
    assert turn_usage[-1]["output_tokens"] == 200


def test_settle_committing_drops_only_the_latest_entry():
    agent = SimpleNamespace(session_api_calls=0, session_estimated_cost_usd=0.0)
    older = {"response": _response(usage=_REJECTED_USAGE), "provider": "openai",
             "model": "gpt-4o", "base_url": "", "api_mode": "chat_completions",
             "turn_id": "t1"}
    latest = dict(older, response=_response(usage=_ACCEPTED_USAGE))
    agent._billed_unaccounted = [older, latest]
    calls = []

    settled = _settle_unaccepted_billed_responses(agent, calls, "t1", committing=True)

    assert settled == 1
    assert agent._billed_unaccounted == []
    assert agent.session_api_calls == 1
    assert agent.session_output_tokens == 10606
    assert [c["output_tokens"] for c in calls] == [10606]


def test_settle_skips_rollup_for_a_foreign_turn():
    agent = SimpleNamespace(session_api_calls=0, session_estimated_cost_usd=0.0)
    agent._billed_unaccounted = [{
        "response": _response(usage=_REJECTED_USAGE), "provider": "openai",
        "model": "gpt-4o", "base_url": "", "api_mode": "chat_completions",
        "turn_id": "old-turn",
    }]
    calls = []

    _settle_unaccepted_billed_responses(agent, calls, "new-turn")

    assert calls == []
    assert agent.session_api_calls == 1
    assert agent.session_output_tokens == 10606
