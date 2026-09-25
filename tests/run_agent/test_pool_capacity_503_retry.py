"""A relay "no eligible sub" 503 waits for a seat BEFORE leaving the provider.

Card t_9d25b5e1 (2026-09-24 sub-vps-16 burst). The claude-apr/-bpr relays
answer::

    HTTP 503 {"error":"no eligible sub for the requested model; this model's
    budget is capped on every subscription while other models are unaffected"}

when every pooled sub is quota-reserved for the requested model. The harness
classified that correctly (``pool_exhausted``) but treated it like any
retryable error: two ~2s/~4s jitters, then ``fallback_providers``. For a
bridge-backed conversation a provider switch is a HOST MOVE — the receiving
box has no CLI session and replays the whole history (measured: 168 replays
of 64k-830k chars on one sub in 10h).

These tests pin the new policy: same-provider retry bounded by
``agent.capacity_retry_attempts`` / ``agent.capacity_retry_max_wait_s``,
relay ``Retry-After`` honoured when it fits the budget, then the existing
fallback path — and ``attempts: 0`` restores the old behaviour exactly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.retry_utils import (
    CAPACITY_RETRY_DEFAULT_ATTEMPTS,
    CAPACITY_RETRY_DEFAULT_MAX_WAIT_S,
    capacity_retry_wait,
)
from run_agent import AIAgent


# Verbatim from claude_pool_relay.py's model-scoped no-eligible return.
POOL_503_BODY = {
    "error": (
        "no eligible sub for the requested model; this model's budget is "
        "capped on every subscription while other models are unaffected"
    )
}


class PoolCapacity503(Exception):
    """Shape of the SDK error the loop sees for a relay 503."""

    def __init__(self, retry_after: str | None = None):
        super().__init__(f"HTTP 503: {POOL_503_BODY['error']}")
        self.status_code = 503
        self.body = POOL_503_BODY
        headers = {"content-type": "application/json"}
        if retry_after is not None:
            headers["retry-after"] = retry_after
        self.response = SimpleNamespace(
            status_code=503,
            headers=headers,
            json=lambda: POOL_503_BODY,
        )


# --------------------------------------------------------------------------
# The policy helper — the pure half.
# --------------------------------------------------------------------------


class TestCapacityRetryWait:
    def test_classifier_premise_holds(self):
        """The loop keys on ``pool_exhausted``; make sure the live body still
        classifies that way (the whole feature is inert otherwise)."""
        c = classify_api_error(PoolCapacity503(), provider="claude-apr")
        assert c.reason is FailoverReason.pool_exhausted
        assert c.retryable is True and c.should_fallback is True

    def test_honors_retry_after_inside_budget(self):
        assert capacity_retry_wait(
            retry_count=1, max_retries=3, raw_retry_after="12",
            waited_s=0.0, max_wait_s=90.0,
        ) == 12.0

    def test_retry_after_beyond_remaining_budget_means_leave_now(self):
        """The relay said the seat frees LATER than we will wait → None."""
        assert capacity_retry_wait(
            retry_count=1, max_retries=3, raw_retry_after="600",
            waited_s=0.0, max_wait_s=90.0,
        ) is None
        # ...and the budget is the REMAINING budget, not the nominal one.
        assert capacity_retry_wait(
            retry_count=1, max_retries=3, raw_retry_after="30",
            waited_s=70.0, max_wait_s=90.0,
        ) is None

    def test_jitter_is_clamped_to_remaining_budget(self):
        with patch("agent.retry_utils.jittered_backoff", return_value=25.0):
            assert capacity_retry_wait(
                retry_count=1, max_retries=3, raw_retry_after=None,
                waited_s=80.0, max_wait_s=90.0,
            ) == 10.0

    def test_wall_clock_budget_exhausted(self):
        assert capacity_retry_wait(
            retry_count=1, max_retries=3, raw_retry_after=None,
            waited_s=90.0, max_wait_s=90.0,
        ) is None

    def test_attempt_budget_exhausted(self):
        assert capacity_retry_wait(
            retry_count=3, max_retries=3, raw_retry_after="1",
            waited_s=0.0, max_wait_s=90.0,
        ) is None

    @pytest.mark.parametrize("raw", [None, "", "soon", "-5", "0"])
    def test_unusable_retry_after_falls_back_to_jitter(self, raw):
        with patch("agent.retry_utils.jittered_backoff", return_value=7.0):
            assert capacity_retry_wait(
                retry_count=1, max_retries=3, raw_retry_after=raw,
                waited_s=0.0, max_wait_s=90.0,
            ) == 7.0

    def test_defaults_are_the_card_numbers(self):
        assert CAPACITY_RETRY_DEFAULT_ATTEMPTS == 3
        assert CAPACITY_RETRY_DEFAULT_MAX_WAIT_S == 90.0


# --------------------------------------------------------------------------
# The loop — real ``run_conversation`` against a stubbed provider.
# --------------------------------------------------------------------------


def _response(content: str, model: str = "claude-test"):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model=model, usage=None)


def _make_agent(statuses: list[tuple[str, str]]) -> AIAgent:
    fallback = [
        {
            "provider": "openrouter",
            "model": "fallback/model",
            "base_url": "https://fallback.example/v1",
        }
    ]
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key",
            base_url="http://127.0.0.1:18810/anthropic",
            provider="claude-apr",
            model="claude-test",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback,
            status_callback=lambda kind, message: statuses.append((kind, message)),
        )
    # chat_completions puts primary AND fallback on the same seam
    # (``_interruptible_api_call``), so one side_effect script drives the
    # whole turn and the call count is the number of provider attempts.
    agent.api_mode = "chat_completions"
    agent._api_max_retries = 3
    return agent


def _fallback_client() -> MagicMock:
    client = MagicMock()
    client.api_key = "fallback-key"
    client.base_url = "https://fallback.example/v1"
    client._custom_headers = None
    client.default_headers = None
    return client


class _FakeClock:
    """``time`` stand-in for the loop: ``sleep`` advances the clock instead
    of blocking, so a 90s capacity budget runs in microseconds and the test
    can assert on the REQUESTED waits. Everything else delegates."""

    def __init__(self, sleeps: list[float]):
        import time as _real

        self._real = _real
        self._offset = 0.0
        self._sleeps = sleeps

    def time(self):
        return self._real.time() + self._offset

    def monotonic(self):
        return self._real.monotonic() + self._offset

    def perf_counter(self):
        return self._real.perf_counter() + self._offset

    def sleep(self, secs):
        self._sleeps.append(float(secs))
        self._offset += float(secs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run(agent, outcomes, sleeps: list[float]):
    """Drive one turn. ``outcomes`` scripts ``_interruptible_api_call`` in
    order (exceptions raise, responses return); the last entry repeats.
    Returns ``(result, activate_mock, call_mock)``."""
    seq = list(outcomes)

    def _fake_call(*_a, **_k):
        outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    clock = _FakeClock(sleeps)
    with (
        # Route the PRIMARY through the same seam as the fallback (the cron /
        # delegated-child inline path) so one script drives the whole turn.
        patch("agent.chat_completion_helpers.should_use_direct_api_call", return_value=True),
        patch.object(agent, "_interruptible_api_call", side_effect=_fake_call) as call,
        patch.object(agent, "_try_activate_fallback", wraps=agent._try_activate_fallback) as activate,
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_fallback_client(), "fallback/model"),
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, _provider: model,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch(
            "hermes_cli.config.read_raw_config",
            return_value={"model": {"announce_route_change": True}},
        ),
        # Generic path jitter (pre-policy) vs the capacity schedule. Pin both
        # so the test asserts on POLICY, not RNG: a 2.0 sleep is the generic
        # path, a 5.0 sleep is the capacity path.
        patch("agent.conversation_loop.jittered_backoff", return_value=2.0),
        patch("agent.retry_utils.jittered_backoff", return_value=5.0),
        # The loop sleeps in 0.2s ticks until ``time.time() >= sleep_end``;
        # the fake clock advances on sleep so the wait is recorded, not served.
        patch("agent.conversation_loop.time", clock),
    ):
        result = agent.run_conversation("hello")
    return result, activate, call


def _waits(sleeps: list[float]) -> list[float]:
    """The loop's backoff sleeps are 0.2s ticks; the retry-wait itself is
    what we care about. Sum ticks between non-tick sleeps is overkill — the
    fake clock jumps ``sleep_end`` on the first tick, so each retry wait
    shows up as exactly one 0.2 tick. Return the count of retry waits."""
    return [s for s in sleeps if s == 0.2]


def test_503_twice_then_served_stays_on_the_same_provider(caplog):
    """The headline: two pool 503s, then a seat frees → served by the
    PRIMARY, no fallback, no host move."""
    caplog.set_level("WARNING", logger="agent.conversation_loop")
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    agent._capacity_retry_attempts = 3
    agent._capacity_retry_max_wait_s = 90.0
    sleeps: list[float] = []

    result, activate, call = _run(
        agent, [PoolCapacity503(), PoolCapacity503(), _response("served")], sleeps
    )

    assert result["completed"] is True
    assert result["final_response"] == "served"
    assert agent._fallback_activated is False
    activate.assert_not_called()
    assert agent.provider == "claude-apr"
    assert call.call_count == 3
    # Buffered status lines are dropped on a successful turn (by design —
    # transient chatter), so the operator-facing evidence is the journal.
    assert "capacity 503 on claude-apr: retry 2/3 in 5.0s (waited 5s of 90s budget)" in caplog.text
    assert "capacity 503 on claude-apr: retry 3/3 in 5.0s (waited 10s of 90s budget)" in caplog.text
    assert "budget exhausted" not in caplog.text
    trace = " || ".join(m for _k, m in statuses)
    assert "Model fallback" not in trace


def test_503_forever_falls_back_after_the_attempt_budget_with_the_pool_reason(caplog):
    """Budget spent → the EXISTING retries-exhausted fallback fires, and it
    announces the honest pool reason (not 'connection issue')."""
    caplog.set_level("WARNING", logger="agent.conversation_loop")
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    agent._capacity_retry_attempts = 3
    agent._capacity_retry_max_wait_s = 90.0
    sleeps: list[float] = []

    err = PoolCapacity503()
    result, activate, call = _run(agent, [err, err, err, _response("via fallback")], sleeps)

    assert result["completed"] is True
    assert result["final_response"] == "via fallback"
    assert agent._fallback_activated is True
    activate.assert_called_once()
    assert activate.call_args.kwargs["reason"] is FailoverReason.pool_exhausted
    # Three same-provider attempts (attempts=3), then ONE fallback call.
    assert call.call_count == 4
    assert "capacity 503 on claude-apr: retry 2/3 in 5.0s (waited 5s of 90s budget)" in caplog.text
    assert "capacity 503 on claude-apr: retry 3/3 in 5.0s (waited 10s of 90s budget)" in caplog.text
    assert "budget exhausted after 10s / 3 attempt(s) (limits attempts=3 max_wait=90s" in caplog.text
    trace = " || ".join(m for _k, m in statuses)
    # The failover announce names the pool reason, not "connection issue".
    assert "🔄 Model fallback (claude-test capped pool-wide" in trace


def test_retry_after_longer_than_the_budget_leaves_immediately():
    """Relay says 600s; we will wait at most 90s → fall back on the FIRST
    503 (one primary attempt), no capacity sleep at all."""
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    agent._capacity_retry_attempts = 3
    agent._capacity_retry_max_wait_s = 90.0
    sleeps: list[float] = []

    result, activate, call = _run(
        agent, [PoolCapacity503(retry_after="600"), _response("via fallback")], sleeps
    )

    assert agent._fallback_activated is True
    activate.assert_called_once()
    assert activate.call_args.kwargs["reason"] is FailoverReason.pool_exhausted
    assert call.call_count == 2  # one primary 503, one fallback call
    assert sleeps == []


def test_retry_after_inside_the_budget_is_honored(caplog):
    """Relay says 12s → the capacity wait is 12s, not the 5s jitter."""
    caplog.set_level("WARNING", logger="agent.conversation_loop")
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    agent._capacity_retry_attempts = 3
    agent._capacity_retry_max_wait_s = 90.0
    sleeps: list[float] = []

    _run(agent, [PoolCapacity503(retry_after="12"), _response("served")], sleeps)

    assert "capacity 503 on claude-apr: retry 2/3 in 12.0s" in caplog.text
    assert agent._fallback_activated is False


def test_attempts_zero_restores_the_pre_policy_behaviour(caplog):
    """Kill switch: ``capacity_retry_attempts: 0`` → generic retries at
    ``api_max_retries`` with the generic 2s jitter, then fallback."""
    caplog.set_level("WARNING", logger="agent.conversation_loop")
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    agent._capacity_retry_attempts = 0
    agent._capacity_retry_max_wait_s = 90.0
    sleeps: list[float] = []

    err = PoolCapacity503()
    result, activate, call = _run(agent, [err, err, err, _response("via fallback")], sleeps)

    assert agent._fallback_activated is True
    activate.assert_called_once()
    assert call.call_count == 4  # api_max_retries (3) + the fallback call
    assert "capacity 503" not in caplog.text
    assert "Retrying API call in 2.0s (attempt 1/3)" in caplog.text
    assert "policy=default" in caplog.text
    assert "policy=pool_capacity" not in caplog.text


def test_attempts_widen_the_retry_ceiling_past_api_max_retries():
    """attempts=5 with api_max_retries=3 → five same-provider tries."""
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    agent._capacity_retry_attempts = 5
    agent._capacity_retry_max_wait_s = 900.0
    sleeps: list[float] = []

    err = PoolCapacity503()
    _, _, call = _run(agent, [err, err, err, err, err, _response("via fallback")], sleeps)

    assert call.call_count == 6
    assert agent._fallback_activated is True


def test_wall_clock_budget_caps_the_same_provider_wait(caplog):
    """attempts=10 but only 8s of budget → two 5s-jitter waits can't fit
    (5 + clamp 3), so the third 503 falls back: 3 primary calls, not 10."""
    caplog.set_level("WARNING", logger="agent.conversation_loop")
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    agent._capacity_retry_attempts = 10
    agent._capacity_retry_max_wait_s = 8.0
    sleeps: list[float] = []

    err = PoolCapacity503()
    _, activate, call = _run(agent, [err, err, err, _response("via fallback")], sleeps)

    assert agent._fallback_activated is True
    activate.assert_called_once()
    assert call.call_count == 4  # 503, wait 5, 503, wait 3, 503 → budget spent → fallback
    assert "retry 2/10 in 5.0s (waited 5s of 8s budget)" in caplog.text
    assert "retry 3/10 in 3.0s (waited 8s of 8s budget)" in caplog.text
    assert "budget exhausted after 8s / 3 attempt(s)" in caplog.text
    assert "policy=pool_capacity" in caplog.text


def test_config_knobs_are_read_from_the_agent_section():
    """``agent.capacity_retry_attempts`` / ``agent.capacity_retry_max_wait_s``
    land on the agent; garbage falls back to the defaults; negatives clamp."""
    def _build(agent_section):
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("hermes_cli.config.load_config_readonly", return_value={"agent": agent_section}),
        ):
            return AIAgent(
                api_key="k", base_url="http://127.0.0.1:18810/anthropic",
                provider="claude-apr", model="claude-test", quiet_mode=True,
                skip_context_files=True, skip_memory=True,
            )

    a = _build({"capacity_retry_attempts": 5, "capacity_retry_max_wait_s": 30})
    assert (a._capacity_retry_attempts, a._capacity_retry_max_wait_s) == (5, 30.0)
    a = _build({"capacity_retry_attempts": "nope", "capacity_retry_max_wait_s": "x"})
    assert (a._capacity_retry_attempts, a._capacity_retry_max_wait_s) == (
        CAPACITY_RETRY_DEFAULT_ATTEMPTS, CAPACITY_RETRY_DEFAULT_MAX_WAIT_S)
    a = _build({"capacity_retry_attempts": -2, "capacity_retry_max_wait_s": -1})
    assert (a._capacity_retry_attempts, a._capacity_retry_max_wait_s) == (0, 0.0)
    a = _build({})
    assert (a._capacity_retry_attempts, a._capacity_retry_max_wait_s) == (
        CAPACITY_RETRY_DEFAULT_ATTEMPTS, CAPACITY_RETRY_DEFAULT_MAX_WAIT_S)
