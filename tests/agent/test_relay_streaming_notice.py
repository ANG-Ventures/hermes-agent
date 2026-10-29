"""t_8d6b8e30: the "buffering lane" notice follows the relay's own `x-relay-streaming`.

A claude-pool face with `stream_after_commit` on streams after the commit point and
stamps `x-relay-streaming: 1` on every response. The notice fires before the current
attempt's head exists (the relay sends its head at commit), so the harness learns it
from the relay's previous responses, keyed by relay origin (one relay process per port).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from agent import fallback_wall_budget as wb
from agent.chat_completion_helpers import _snapshot_pool_headers
from agent.fork_ext import relay_headers as rh

CFG = {"fallback": {"still_waiting_notice_seconds": 120, "no_first_byte_budget_seconds": 600}}


@pytest.fixture(autouse=True)
def _clean_relay_streaming():
    reset = getattr(rh, "reset_relay_streaming", lambda: None)
    reset()
    yield
    reset()


def _resp(url: str, headers: dict) -> httpx.Response:
    return httpx.Response(200, headers=headers, request=httpx.Request("POST", url))


def _agent(provider: str, base_url: str):
    return SimpleNamespace(provider=provider, model="claude-fable-5-1", base_url=base_url,
                           _fallback_chain=[], _fallback_index=0, _wall_budget=None)


def _notice(agent):
    with patch("hermes_cli.config.load_config_readonly", return_value=CFG):
        wb.arm(agent, now=0.0)
        try:
            return wb.notice_due(agent, now=121.0)
        finally:
            # One solo wait per call: a wait left armed would read as a sibling session
            # on the same lane (t_78b5a5bb cross-session dedupe).
            wb.disarm(agent, now=121.0)


def test_streaming_relay_drops_the_buffering_wording():
    a = _agent("claude-alr", "http://127.0.0.1:18810/v1")
    assert "buffering lane" in _notice(a)  # nothing seen yet: today's wording
    _snapshot_pool_headers(_resp("http://127.0.0.1:18810/v1/messages",
                                 {"x-pool-served-by": "sub-vps-1", "x-relay-streaming": "1"}))
    text = _notice(_agent("claude-alr", "http://127.0.0.1:18810/v1"))
    assert text.startswith("⏳ still waiting for a first reply: 121s"), text
    assert "buffering" not in text


def test_rollback_restores_the_buffering_wording():
    _snapshot_pool_headers(_resp("http://127.0.0.1:18810/v1/messages", {"x-relay-streaming": "1"}))
    assert "buffering" not in _notice(_agent("claude-alr", "http://127.0.0.1:18810/v1"))
    _snapshot_pool_headers(_resp("http://127.0.0.1:18810/v1/messages",
                                 {"x-pool-served-by": "sub-vps-1", "x-pool-route-id": "r1"}))
    assert "buffering lane" in _notice(_agent("claude-alr", "http://127.0.0.1:18810/v1"))


def test_streaming_is_per_relay_origin_not_per_family():
    _snapshot_pool_headers(_resp("http://127.0.0.1:18810/anthropic/v1/messages",
                                 {"x-relay-streaming": "1"}))
    # Same relay, other face name and path: streams.
    assert "buffering" not in _notice(_agent("claude-apr", "http://localhost:18810/anthropic"))
    # The bpr relay (another process, flag off) still buffers.
    assert "buffering lane" in _notice(_agent("claude-bpr", "http://127.0.0.1:18811/v1"))


def test_header_from_a_non_relay_origin_never_changes_a_relay_lane():
    _snapshot_pool_headers(_resp("https://api.example.com/v1/messages", {"x-relay-streaming": "1"}))
    assert "buffering lane" in _notice(_agent("claude-alr", "http://127.0.0.1:18810/v1"))
