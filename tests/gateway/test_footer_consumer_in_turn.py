"""Consumer half of the runtime-footer provider wiring: the footer Ace actually sees.

Regression (2026-10-04, t_829a3079): the upstream split of the turn path out of
``gateway/run.py`` into ``gateway/run_turn.py`` kept the ``build_footer_line(...)`` call
shell but dropped six fork kwargs (``provider``, ``context_tokens`` via
``_footer_context_tokens``, ``context_estimated``, ``reasoning``, ``message_count``,
``message_limit``). The live Discord footer degraded from
``claude-alr/claude-fable-5-1 · r:medium · …`` to a bare ``claude-fable-5-1 · …``.
The producer contract stayed green the whole time, because it only checks the producer.

PAIRED TESTS. The PRODUCER half (the turn result carries ``provider``) lives in
``tests/gateway/test_footer_provider_in_turn_result.py``. This file covers the CONSUMER
half (``GatewayTurnMixin._hmwa_runtime_footer_line`` in ``gateway/run_turn.py``). The
next split that moves either half must move both tests.

These tests drive the real consumer method and render or capture the real
``build_footer_line`` call. They never read source text, so they survive a correct
refactor and fail on a call that is wired wrong.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import gateway.run as gw_run
import gateway.runtime_footer as rf
from gateway.config import Platform
from gateway.session import SessionSource

# Ace's default-profile footer fields (~/.hermes/config.yaml display.runtime_footer).
_DEFAULT_PROFILE_FIELDS = ["provider_model", "reasoning", "context_full", "messages", "latency", "cwd"]

# Floor: every kwarg the fork passed to build_footer_line at the pre-split consumer
# (gateway/run.py @ 2eb646f7, ~line 28344). A later call may pass MORE; it may never pass fewer.
_PRE_SPLIT_FORK_KWARGS = frozenset({
    "user_config", "platform_key", "model", "provider", "context_tokens",
    "context_estimated", "context_length", "cwd", "reasoning",
    "message_count", "message_limit", "turn_seconds",
})

_WHERE = (
    "consumer = GatewayTurnMixin._hmwa_runtime_footer_line (gateway/run_turn.py); "
    "producer = TurnRunner.run_sync usage dict (gateway/run_turn_runner.py), pinned by "
    "tests/gateway/test_footer_provider_in_turn_result.py"
)


def _runner(message_count=42):
    runner = object.__new__(gw_run.GatewayRunner)
    runner._session_reasoning_overrides = {}

    class _DB:
        async def get_session(self, session_id):
            return {"message_count": message_count} if session_id == "sess-1" else None

    runner._session_db = _DB()
    return runner


def _source():
    return SessionSource(platform=Platform.DISCORD, user_id="u1", chat_id="c1", user_name="ace")


def _agent_result(provider, model):
    return {
        "provider": provider,
        "model": model,
        "last_prompt_tokens": 405_000,
        "context_length": 1_000_000,
        "reasoning_config": {"enabled": True, "effort": "medium"},
    }


def _config():
    return {
        "display": {"runtime_footer": {"enabled": True, "fields": list(_DEFAULT_PROFILE_FIELDS)}},
        "compression": {"enabled": True, "hygiene_hard_message_limit": 400},
    }


def _call_consumer(runner, agent_result, source, turn_seconds=380.0):
    """Drive the consumer the way the turn does: pass the session context it accepts."""
    method = runner._hmwa_runtime_footer_line
    extra = {"session_entry": SimpleNamespace(session_id="sess-1"), "session_key": "agent:main:discord:c1"}
    params = inspect.signature(method).parameters
    kwargs = {k: v for k, v in extra.items() if k in params}
    out = method(agent_result, source, turn_seconds, **kwargs)
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    return out


@pytest.fixture
def footer_env(monkeypatch):
    monkeypatch.setattr(gw_run, "_load_gateway_config", _config)
    monkeypatch.setattr(gw_run, "_terminal_scope_cwd", lambda default="": "")


# --- a. behavioural end-to-end: the rendered string ---------------------------------------

@pytest.mark.parametrize(
    "provider,model,expected_head",
    [
        ("claude-alr", "claude-fable-5-1", "claude-alr/claude-fable-5-1 · "),
        ("openai-codex", "gpt-6-astra", "openai-codex/gpt-6-astra · "),
        ("openrouter", "moonshotai/kimi-k3", "openrouter/moonshotai/kimi-k3 · "),
        # No provider on the result: bare model is the ONLY acceptable degradation.
        (None, "claude-fable-5-1", "claude-fable-5-1 · "),
    ],
)
def test_rendered_footer_leads_with_provider_model(footer_env, provider, model, expected_head):
    line = _call_consumer(_runner(), _agent_result(provider, model), _source())
    assert line.startswith(expected_head), (
        f"rendered footer {line!r} does not start with {expected_head!r}. The CONSUMER dropped "
        f"provider=agent_result.get('provider') (or the producer stopped setting it). {_WHERE}"
    )
    assert "r:medium" in line, f"live reasoning label missing from {line!r}. {_WHERE}"
    assert "/1M (40%)" in line, f"context figure missing from {line!r}. {_WHERE}"
    assert "42/400msgs" in line, f"message stats missing from {line!r}. {_WHERE}"
    assert "6m20s" in line, f"latency missing from {line!r}. {_WHERE}"


def test_rendered_footer_prefers_post_compaction_context_figure(footer_env):
    """``context_tokens_display`` (post-compaction) wins over raw ``last_prompt_tokens``."""
    result = _agent_result("claude-alr", "claude-fable-5-1")
    result.update(context_tokens_display=95_100, context_tokens_estimated=True)
    line = _call_consumer(_runner(), result, _source())
    assert "~95.1k/1M" in line, (
        f"footer {line!r} ignored context_tokens_display / context_tokens_estimated; the consumer "
        f"must pass context_tokens=_footer_context_tokens(agent_result) and context_estimated. {_WHERE}"
    )


# --- b + c. consumer call contract and kwarg parity (captured call, no source reading) ------

@pytest.fixture
def captured_call(footer_env, monkeypatch):
    calls = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return "spy"

    monkeypatch.setattr(rf, "build_footer_line", _spy)
    _call_consumer(_runner(), _agent_result("claude-alr", "claude-fable-5-1"), _source())
    assert len(calls) == 1, f"consumer made {len(calls)} build_footer_line calls, expected 1. {_WHERE}"
    return calls[0]


def test_consumer_passes_provider_from_turn_result(captured_call):
    assert captured_call.get("provider") == "claude-alr", (
        "the consumer's build_footer_line call does not pass provider=agent_result.get('provider'). "
        f"The producer still sets it; the footer renders the bare model. {_WHERE}"
    )


def test_consumer_kwargs_are_superset_of_pre_split_fork_call(captured_call):
    dropped = sorted(_PRE_SPLIT_FORK_KWARGS - set(captured_call))
    assert not dropped, (
        f"build_footer_line call lost fork kwarg(s) {dropped} compared with the pre-split call in "
        f"gateway/run.py @ 2eb646f7. Upstream moved the call site and the merge kept the shell. {_WHERE}"
    )
