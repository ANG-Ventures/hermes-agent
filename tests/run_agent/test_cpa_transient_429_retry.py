"""t_28826b21: a cpa transient upstream 429 retries in place before falling back.

cpa (CLIProxyAPI) answers 503 ``auth_unavailable: no auth available (...; last
upstream error: 429 ...)`` once its own request-retry is spent. Before the fix
that body mapped to ``_V_RELAY_RATE_LIMIT`` (should_fallback=True) and
turn_recovery's ``is_rate_limited`` switched provider on the FIRST failure:
zero in-place retries and a broken prompt cache. Now it backs off on the same
route up to ``api_max_retries`` and falls back with the honest label after.
A ``credential_quota`` cooldown (a multi-day wall) still falls back on the
first error; the bare no-credential body is unchanged.

Rig: the real ``run_conversation`` loop, primary and fallback on one
``_interruptible_api_call`` seam, a fake clock so backoff is recorded, not
served (same shape as test_pool_capacity_503_retry.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from run_agent import AIAgent

CPA_URL = "http://127.0.0.1:18812/v1"
_BASE = "auth_unavailable: no auth available (providers=codex, model=gpt-6-astra; "
RATE_429 = _BASE + "last upstream error: 429 Too Many Requests: rate_limit_exceeded)"
# Upstream summary kept short so "account" survives the 120-char provider_message
# cap the cause renderer reads ("account rate limit" only when the vendor says so).
RATE_429_ACCOUNT = ("auth_unavailable: no auth available (providers=codex, model=m; "
                    "last upstream error: 429: exceeded your account's rate limit)")
QUOTA_COOLDOWN = _BASE + "cooldown: credential_quota until 2026-10-19)"
BARE = "auth_unavailable: no auth available (providers=kimi, model=kimi-k3)"


class Cpa503(Exception):
    """Shape of the SDK error the loop sees for a cpa 503."""

    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(f"Error code: 503 - {message}")
        self.status_code = 503
        self.body = {"message": message, "type": "server_error", "code": "internal_server_error"}
        headers = {"content-type": "application/json"}
        if retry_after is not None:
            headers["retry-after"] = retry_after
        self.response = SimpleNamespace(status_code=503, headers=headers, json=lambda: {"error": self.body})


def _response(content: str):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="gpt-6-astra", usage=None)


def _make_agent(statuses: list) -> AIAgent:
    fallback = [{"provider": "openrouter", "model": "fallback/model", "base_url": "https://fallback.example/v1"}]
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="cpa-proxy-key", base_url=CPA_URL, provider="cpa", model="gpt-6-astra",
            quiet_mode=True, skip_context_files=True, skip_memory=True, fallback_model=fallback,
            status_callback=lambda kind, message: statuses.append((kind, message)),
        )
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
    """``time`` stand-in: ``sleep`` advances a frozen clock and is recorded."""

    def __init__(self, sleeps: list):
        import time as _real

        self._real, self._offset, self._sleeps = _real, 0.0, sleeps
        self._t0, self._m0, self._p0 = _real.time(), _real.monotonic(), _real.perf_counter()

    def time(self):
        return self._t0 + self._offset

    def monotonic(self):
        return self._m0 + self._offset

    def perf_counter(self):
        return self._p0 + self._offset

    def sleep(self, secs):
        self._sleeps.append(float(secs))
        self._offset += float(secs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run(agent, outcomes, sleeps: list, backoff=None, providers: list | None = None):
    seq = list(outcomes)

    def _fake_call(*_a, **_k):
        if providers is not None:
            providers.append(agent.provider)
        outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    clock = _FakeClock(sleeps)
    agent._disable_streaming = True
    with (
        patch("agent.chat_completion_helpers.should_use_direct_api_call", return_value=True),
        patch.object(agent, "_interruptible_api_call", side_effect=_fake_call) as call,
        patch.object(agent, "_try_activate_fallback", wraps=agent._try_activate_fallback) as activate,
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(_fallback_client(), "fallback/model")),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda model, _p: model),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("hermes_cli.config.read_raw_config", return_value={"model": {"announce_route_change": True}}),
        patch("agent.retry_utils.jittered_backoff", side_effect=backoff or (lambda *_a, **_k: 2.0)),
        patch("agent.turn_recovery.time", clock),
    ):
        result = agent.run_conversation("hello")
    return result, activate, call


def test_classifier_premise():
    c = classify_api_error(Cpa503(RATE_429), provider="cpa", model="gpt-6-astra")
    assert c.reason is FailoverReason.rate_limit
    assert c.retryable is True and c.should_fallback is False and c.should_rotate_credential is False


def test_429_twice_then_served_stays_on_cpa():
    statuses: list = []
    agent = _make_agent(statuses)
    result, activate, call = _run(agent, [Cpa503(RATE_429), Cpa503(RATE_429), _response("served")], [])
    assert result["completed"] is True
    assert result["final_response"] == "served"
    activate.assert_not_called()
    assert agent._fallback_activated is False
    assert agent.provider == "cpa"
    assert call.call_count == 3


def test_429_forever_retries_three_times_then_falls_back_with_rate_limit_label():
    statuses: list = []
    agent = _make_agent(statuses)
    err = Cpa503(RATE_429_ACCOUNT)
    providers: list = []
    result, activate, call = _run(agent, [err, err, err, _response("via fallback")], [], providers=providers)
    assert result["completed"] is True
    assert result["final_response"] == "via fallback"
    activate.assert_called_once()
    assert activate.call_args.kwargs["reason"] is FailoverReason.rate_limit
    # 3 primary attempts (api_max_retries) before the one fallback call.
    assert call.call_count == 4
    assert providers == ["cpa", "cpa", "cpa", "openrouter"]
    trace = " || ".join(m for _k, m in statuses)
    assert "Model fallback (rate limit): cpa/gpt-6-astra" in trace
    assert "— account rate limit;" in trace


def test_retry_after_within_cap_is_honoured():
    statuses: list = []
    agent = _make_agent(statuses)
    sleeps: list = []
    result, activate, call = _run(agent, [Cpa503(RATE_429, retry_after="7"), _response("served")], sleeps)
    assert result["final_response"] == "served"
    activate.assert_not_called()
    assert call.call_count == 2
    # The loop sleeps in 0.2s ticks until the deadline; the honoured wait is their sum.
    assert sum(sleeps) == pytest.approx(7.0, abs=0.3)


def test_retry_after_beyond_cap_falls_back_now():
    statuses: list = []
    agent = _make_agent(statuses)
    sleeps: list = []
    result, activate, call = _run(agent, [Cpa503(RATE_429, retry_after="300"), _response("via fallback")], sleeps)
    assert result["final_response"] == "via fallback"
    activate.assert_called_once()
    assert call.call_count == 2
    assert sum(sleeps) < 1.0


def test_credential_quota_cooldown_falls_back_on_the_first_error():
    statuses: list = []
    agent = _make_agent(statuses)
    result, activate, call = _run(agent, [Cpa503(QUOTA_COOLDOWN), _response("via fallback")], [])
    assert result["final_response"] == "via fallback"
    activate.assert_called_once()
    assert call.call_count == 2


def test_bare_auth_unavailable_negative_control_falls_back_on_the_first_error():
    statuses: list = []
    agent = _make_agent(statuses)
    result, activate, call = _run(agent, [Cpa503(BARE), _response("via fallback")], [])
    assert result["final_response"] == "via fallback"
    activate.assert_called_once()
    assert activate.call_args.kwargs["reason"] is FailoverReason.auth_permanent
    assert call.call_count == 2


def test_direct_provider_and_relay_header_429_verdicts_unchanged():
    """Out of scope paths keep their verdicts: a direct 429 and a claude relay's stated
    ``rate_upstream`` class still fall back eagerly, with no cpa marker."""
    from agent.error_classifier import CPA_TRANSIENT_429

    direct = Cpa503("Rate limit exceeded")
    direct.status_code = 429
    direct.response.status_code = 429
    c = classify_api_error(direct, provider="openrouter", model="m")
    assert c.reason is FailoverReason.rate_limit and c.should_fallback is True
    assert CPA_TRANSIENT_429 not in c.error_context

    relay = Cpa503("x")
    relay.status_code = 429
    relay.response.headers["x-relay-error-class"] = "rate_upstream"
    c = classify_api_error(relay, provider="claude-apr", model="m")
    assert c.reason is FailoverReason.rate_limit and c.should_fallback is True
    assert CPA_TRANSIENT_429 not in c.error_context
