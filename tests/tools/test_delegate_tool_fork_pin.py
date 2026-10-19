"""Boomerang v2 P1 (spec D-3 / INV-3 / INV-8): a FORK child's FIRST request carries
``x-hermes-pin-sub: <parent.last_served_by>``; later requests and every fold child
carry no pin. Driven through the real request builder (``AIAgent._build_api_kwargs``
on the anthropic_messages route)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tools.delegate_tool import _arm_first_request_pin, _should_pin_first_request

_PIN = "x-hermes-pin-sub"
_MSGS = [{"role": "user", "content": "hi"}]


def _child(provider="claude-alr"):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="k", base_url="http://127.0.0.1:18810/anthropic", provider=provider,
            model="claude-test", api_mode="anthropic_messages", quiet_mode=True,
            skip_context_files=True, skip_memory=True, session_id="20261018_170000_aaaaaa",
        )
    agent.api_mode = "anthropic_messages"
    agent.provider = provider
    agent._delegate_depth = 1
    return agent


def _headers(agent) -> dict:
    return dict(agent._build_api_kwargs(list(_MSGS)).get("extra_headers") or {})


def _parent(seat="sub-vps-16"):
    return SimpleNamespace(last_served_by=seat)


def test_fork_first_request_pins_parent_seat():
    child = _child()
    _arm_first_request_pin(child, _parent(), "fork")
    assert _headers(child)[_PIN] == "sub-vps-16"


def test_fork_second_request_unpinned():
    child = _child()
    _arm_first_request_pin(child, _parent(), "fork")
    assert _PIN in _headers(child)
    second = _headers(child)
    assert _PIN not in second
    # Still a pool request: affinity headers keep flowing, only the pin is one-shot.
    assert "x-hermes-session" in second


def test_fold_child_carries_no_pin():
    assert _should_pin_first_request(_parent(), "fold") is False
    child = _child()
    _arm_first_request_pin(child, _parent(), "fold")
    assert _PIN not in _headers(child)


@pytest.mark.parametrize("parent", [SimpleNamespace(), _parent(None), _parent(""), _parent("bad seat")])
def test_no_pin_when_seat_unknown(parent):
    assert _should_pin_first_request(parent, "fork") is False
    child = _child()
    _arm_first_request_pin(child, parent, "fork")
    assert _PIN not in _headers(child)


def test_pin_never_reaches_non_pool_provider():
    child = _child(provider="anthropic")
    _arm_first_request_pin(child, _parent(), "fork")
    assert _PIN not in _headers(child)
    # Consumed anyway: a later switch onto a pool lane must not resurrect it.
    child.provider = "claude-alr"
    assert _PIN not in _headers(child)


def test_unarmed_child_build_is_unchanged():
    # INV-1: no new attribute on the child -> the built headers carry no pin.
    assert _PIN not in _headers(_child())
