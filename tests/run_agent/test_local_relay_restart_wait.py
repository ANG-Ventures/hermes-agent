"""A connection error on a LOOPBACK relay (claude-bpr :18811 restarting under
relay-autodeploy) must wait for the listener to come back and retry the SAME
model, not walk the fallback chain for a 5-second local restart.

Contract:
  * loopback base_url + listener returns inside the window -> same model
    retried, no fallback activation (no fallback_events row, no banner);
  * loopback base_url + listener never returns -> existing conn fallback;
  * non-loopback base_url -> unchanged, no polling.
"""

from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from agent import retry_utils
from run_agent import AIAgent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Listener:
    def __init__(self, port: int):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)

    def close(self):
        self.sock.close()


def _conn_error(base_url: str) -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", base_url))


def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="m", usage=None)


def _make_agent(base_url: str, provider: str, model: str, fb_chain):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="k-abcdef123456",
            base_url=base_url,
            provider=provider,
            model=model,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fb_chain,
        )
        agent.client = MagicMock()
        return agent


def _run(agent, fake_api_call, fb_client_base, *, wait_s):
    mock_fb_client = MagicMock()
    mock_fb_client.api_key = "k-abcdef123456"
    mock_fb_client.base_url = fb_client_base
    mock_fb_client._custom_headers = None
    mock_fb_client.default_headers = None
    activate = MagicMock(wraps=agent._try_activate_fallback)
    waiter = MagicMock(wraps=retry_utils.wait_for_local_relay)
    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_try_activate_fallback", activate),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        # create=True so the contract runs (RED) on a tree without the fix.
        patch("agent.conversation_loop.wait_for_local_relay", waiter, create=True),
        patch("agent.fallback_wiring.local_relay_restart_wait_s", return_value=wait_s),
        patch("agent.conversation_loop.jittered_backoff", return_value=0.05),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_fb_client, "fallback-model"),
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda m, p: m,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")
    return result, activate, waiter


FB_CHAIN = [{"provider": "openrouter", "model": "fallback-model",
             "base_url": "https://openrouter.ai/api/v1"}]


def test_loopback_relay_back_within_window_retries_same_model():
    """The 2026-09-28 08:15 incident: relay listener gone ~1.5 s (restart).
    The fake provider fails exactly while the real loopback port is closed."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}/v1"
    agent = _make_agent(base, "custom", "claude-fable-5-1", FB_CHAIN)
    agent._api_max_retries = 3
    calls = []
    holder = {}

    def _restart_relay():
        time.sleep(1.5)
        holder["l"] = _Listener(port)

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if agent.model != "claude-fable-5-1":
            return _mock_response("served by fallback")
        if "l" not in holder:
            if len(calls) == 1:
                threading.Thread(target=_restart_relay, daemon=True).start()
            raise _conn_error(base)
        return _mock_response("served by the pinned model")

    try:
        result, activate, waiter = _run(agent, fake_api_call, base, wait_s=10.0)
    finally:
        if "l" in holder:
            holder["l"].close()

    assert result["completed"] is True
    assert result["final_response"] == "served by the pinned model"
    assert calls == [("custom", "claude-fable-5-1")] * 2
    activate.assert_not_called()
    assert agent._fallback_activated is False


def test_loopback_relay_never_returns_keeps_conn_fallback():
    port = _free_port()
    base = f"http://127.0.0.1:{port}/v1"
    agent = _make_agent(base, "custom", "claude-fable-5-1", FB_CHAIN)
    agent._api_max_retries = 3
    calls = []

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if agent.model == "claude-fable-5-1":
            raise _conn_error(base)
        return _mock_response("served by fallback")

    t0 = time.monotonic()
    result, activate, waiter = _run(agent, fake_api_call, base, wait_s=1.5)
    elapsed = time.monotonic() - t0

    assert result["final_response"] == "served by fallback"
    assert calls[:2] == [("custom", "claude-fable-5-1")] * 2
    assert calls[-1][1] == "fallback-model"
    assert activate.call_count >= 1
    # The wait is bounded by the configured window across the whole turn.
    assert elapsed < 1.5 + 5.0


def test_non_loopback_base_url_does_not_poll():
    base = "https://openrouter.ai/api/v1"
    agent = _make_agent(base, "openrouter", "some/model", FB_CHAIN)
    agent._api_max_retries = 3
    calls = []

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if agent.model == "some/model":
            raise _conn_error(base)
        return _mock_response("served by fallback")

    result, activate, waiter = _run(agent, fake_api_call, base, wait_s=10.0)
    assert result["final_response"] == "served by fallback"
    waiter.assert_not_called()
    assert activate.call_count >= 1


# ── pure helpers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:18811/v1", True),
    ("http://localhost:8317", True),
    ("http://[::1]:9000/v1", True),
    ("http://127.3.4.5/x", True),
    ("https://openrouter.ai/api/v1", False),
    ("http://192.168.1.10:18811", False),
    ("", False),
    (None, False),
])
def test_is_loopback_base_url(url, expected):
    assert retry_utils.is_loopback_base_url(url) is expected


def test_timeout_is_not_a_restart_signal():
    base = "http://127.0.0.1:18811/v1"
    req = httpx.Request("POST", base)
    assert retry_utils.is_local_relay_restart_candidate(_conn_error(base), base)
    assert not retry_utils.is_local_relay_restart_candidate(
        openai.APITimeoutError(request=req), base)
    assert not retry_utils.is_local_relay_restart_candidate(
        _conn_error(base), "https://openrouter.ai/api/v1")


def test_wait_up_on_first_probe_is_not_a_recovery():
    back, waited = retry_utils.wait_for_local_relay(
        "http://127.0.0.1:1/v1", 5.0, probe=lambda: True, sleep=lambda s: None)
    assert (back, waited) == (False, 0.0)


def test_wait_down_then_up_recovers_and_is_bounded():
    seq = iter([False, False, True])
    slept = []
    back, waited = retry_utils.wait_for_local_relay(
        "http://127.0.0.1:1/v1", 5.0, probe=lambda: next(seq), sleep=slept.append)
    assert back is True and waited == 2.0 and slept == [1.0, 1.0]

    slept = []
    back, waited = retry_utils.wait_for_local_relay(
        "http://127.0.0.1:1/v1", 2.5, probe=lambda: False, sleep=slept.append)
    assert back is False and waited == 2.5 and sum(slept) == 2.5
