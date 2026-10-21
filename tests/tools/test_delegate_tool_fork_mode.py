"""Boomerang v2 P2 (spec 2026-10-18 §5.1/§5.2): ``inherit_mode="fork"``.

INV-2: a fork child's first request is byte-identical to the parent's next request
through the parent's last complete turn, or the fork downgrades to the v1 fold with
``boomerang.fork_ineligible reason=<...>``. Driven through the REAL request path
(``AIAgent.run_conversation`` -> assemble -> ``_build_api_kwargs`` -> transport) with
only the provider call patched.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tools import delegate_tool_fork as fork
from tools.delegate_tool import _build_child_agent

_TOOLS = [
    {"type": "function", "function": {"name": n, "description": f"{n} tool",
                                       "parameters": {"type": "object", "properties": {}}}}
    for n in ("read_file", "terminal")
]
_SEAT = "sub-vps-16"
_PIN = "x-hermes-pin-sub"


def _anthropic_response(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", stop_sequence=None,
        model="claude-fable-5-1", usage=None,
    )


def _patches(stack):
    stack.enter_context(patch("model_tools.get_tool_definitions", return_value=copy.deepcopy(_TOOLS)))
    stack.enter_context(patch("model_tools.check_toolset_requirements", return_value={}))
    stack.enter_context(patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()))
    stack.enter_context(patch("run_agent.OpenAI", return_value=MagicMock()))


def _wire_agent(agent, model="claude-fable-5-1"):
    agent.provider, agent.model, agent.api_mode = "claude-alr", model, "anthropic_messages"
    agent.base_url = agent._anthropic_base_url = "http://127.0.0.1:18810/anthropic"
    agent._anthropic_api_key = "test-key"  # gitleaks:allow (dummy)
    agent._anthropic_client = MagicMock()
    agent._is_anthropic_oauth = False
    agent.client = MagicMock()
    agent.save_trajectories = False
    return agent


def _make_parent():
    with ExitStack() as stack:
        _patches(stack)
        parent = AIAgent(
            api_key="test-key", base_url="http://127.0.0.1:18810/anthropic", provider="claude-alr",
            model="claude-fable-5-1", api_mode="anthropic_messages", quiet_mode=True,
            skip_context_files=True, skip_memory=True, session_id="20261018_200000_parent",
        )
    _wire_agent(parent)
    parent._cache_ttl = "1h"
    parent.last_served_by = _SEAT
    parent.last_served_at = time.time() - 60
    parent._last_cache_creation_split = {"1h": 1000, "5m": 0}
    return parent


def _run(agent, text, history=None):
    """One real turn; returns (result, [captured provider kwargs])."""
    captured = []

    def _create(kwargs, **_kw):
        captured.append(copy.deepcopy(kwargs))
        return _anthropic_response(f"reply to {text[:20]}")

    with ExitStack() as stack:
        _patches(stack)
        stack.enter_context(patch.object(agent, "_create_request_anthropic_client", return_value=MagicMock()))
        stack.enter_context(patch.object(agent, "_anthropic_messages_create", side_effect=_create))
        stack.enter_context(patch.object(agent, "_persist_session"))
        stack.enter_context(patch.object(agent, "_save_trajectory"))
        stack.enter_context(patch.object(agent, "_cleanup_task_resources"))
        kwargs = {} if history is None else {"conversation_history": history}
        result = agent.run_conversation(text, **kwargs)
    return result, captured


def _warm_parent():
    """A parent with one completed turn whose live transcript is on ``_session_messages``."""
    parent = _make_parent()
    result, _ = _run(parent, "The Kestrel canary is at 12 percent.")
    parent._session_messages = result["messages"]
    return parent


def _build(parent, *, mode="fork", model=None, cfg=None):
    with ExitStack() as stack:
        _patches(stack)
        stack.enter_context(patch("tools.delegate_tool._load_config", return_value=cfg or {}))
        child = _build_child_agent(
            task_index=0, goal="verify the canary", context=None, toolsets=None, model=model,
            max_iterations=5, task_count=1, parent_agent=parent, inherit_mode=mode,
        )
    return _wire_agent(child, model=child.model) if child.provider == "claude-alr" else child


def _child_first_request(child, goal="verify the canary"):
    user_message, history = fork.fork_run_inputs(child, goal)
    _, captured = _run(child, user_message, history)
    return captured[0]


def _prefix_digest(kwargs):
    """sha256 over everything the cache prefix covers: system, tools, thinking, model,
    and the message array through the parent's last turn (the trailing user row is the
    only part that may differ)."""
    body = {k: kwargs.get(k) for k in ("model", "system", "tools", "thinking", "max_tokens")}
    body["messages"] = kwargs["messages"][:-1]
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


# ── INV-2 byte identity ────────────────────────────────────────────────────


def test_fork_first_request_prefix_byte_identical_to_parent_next_request():
    parent = _warm_parent()
    child = _build(parent)
    assert getattr(child, "_inherit_mode", None) == "fork"
    child_req = _child_first_request(child)
    _, parent_next = _run(parent, "and the next thing", parent._session_messages)
    assert _prefix_digest(child_req) == _prefix_digest(parent_next[0])
    assert child_req["messages"][-1]["role"] == "user"
    tail = json.dumps(child_req["messages"][-1], default=str)
    assert "BOOMERANG FORK" in tail and "verify the canary" in tail
    assert child_req["extra_headers"][_PIN] == _SEAT


def test_fork_prefix_stops_at_last_complete_assistant_turn():
    """D-8: a delegate_task issued mid-turn sees the in-flight user row (and possibly an
    unfinished tool round) after the last complete assistant turn; none of it is copied."""
    parent = _warm_parent()
    completed = list(parent._session_messages)
    parent._session_messages = completed + [
        {"role": "user", "content": "IN_FLIGHT_MARKER now delegate it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c9", "type": "function", "function": {"name": "delegate_task", "arguments": "{}"}}]},
    ]
    child = _build(parent)
    assert child._inherit_mode == "fork"
    child_req = _child_first_request(child)
    assert "IN_FLIGHT_MARKER" not in json.dumps(child_req["messages"], default=str)
    _, parent_next = _run(parent, "and the next thing", completed)
    assert _prefix_digest(child_req) == _prefix_digest(parent_next[0])


def test_one_system_byte_mutation_downgrades_with_log_line(caplog):
    parent = _warm_parent()
    real_overlay = fork.apply_fork_overlay

    def _mutating_overlay(child, parent_agent):
        undo = real_overlay(child, parent_agent)
        child._cached_system_prompt = parent_agent._cached_system_prompt[:-1] + "X"
        return undo

    caplog.set_level(logging.INFO, logger="tools.delegate_tool")
    with patch.object(fork, "apply_fork_overlay", _mutating_overlay):
        child = _build(parent)
    assert getattr(child, "_inherit_mode", None) != "fork"
    assert getattr(child, "_fork_history", None) is None
    assert "boomerang.fork_ineligible reason=same_request_params:system_prompt" in caplog.text
    pf = child.prefill_messages
    assert isinstance(pf, list) and len(pf) == 1 and "INHERITED CONTEXT FROM PARENT SESSION" in pf[0]["content"]


# ── AC-10: forced parent model ─────────────────────────────────────────────


def test_fork_forces_parent_model_over_delegation_model():
    parent = _warm_parent()
    child = _build(parent, model="claude-opus-5-5", cfg={"model": "claude-opus-5-5"})
    assert getattr(child, "_inherit_mode", None) == "fork"
    assert _child_first_request(child)["model"] == "claude-fable-5-1"


# ── one test per eligibility reason ────────────────────────────────────────


def _eligible_parent(**overrides):
    parent = SimpleNamespace(
        provider="claude-alr", model="claude-fable-5-1", _delegate_depth=0, side_agent=False,
        last_served_by=_SEAT, last_served_at=1_000_000.0, _cache_ttl="1h",
        _last_cache_creation_split={"1h": 10, "5m": 0}, _cached_system_prompt="SYS",
        ephemeral_system_prompt=None, tools=[], session_id="s",
        _session_messages=[{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}],
    )
    for k, v in overrides.items():
        setattr(parent, k, v)
    return parent


_RT = {"provider": "claude-alr", "model": "claude-fable-5-1"}
_NOW = 1_000_000.0 + 60


@pytest.mark.parametrize(
    "overrides,rt,cfg,now,reason",
    [
        ({}, {**_RT, "provider": "openrouter"}, {}, _NOW, "same_lane"),
        ({"provider": "openrouter"}, {**_RT, "provider": "openrouter"}, {}, _NOW, "same_lane"),
        ({}, {**_RT, "model": "claude-opus-5-5"}, {}, _NOW, "same_model"),
        ({"_cached_system_prompt": None}, _RT, {}, _NOW, "same_request_params"),
        ({"_session_messages": [{"role": "user", "content": "u"}]}, _RT, {}, _NOW, "turn_complete"),
        ({"_session_messages": [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "x"}}]},
            {"role": "assistant", "content": "a"},
        ]}, _RT, {}, _NOW, "turn_complete"),
        ({"last_served_by": None}, _RT, {}, _NOW, "seat_known"),
        ({}, _RT, {}, 1_000_000.0 + 56 * 60, "warm"),  # 1h parent at 56 min
        ({"_last_cache_creation_split": {"1h": 0, "5m": 10}}, _RT, {}, 1_000_000.0 + 6 * 60, "warm"),  # 5m at 6 min
        ({"_delegate_depth": 1, "side_agent": True}, _RT, {}, _NOW, "fork_depth"),
        ({}, _RT, {"boomerang": {"fork_enabled": False}}, _NOW, "fork_enabled"),
    ],
)
def test_each_eligibility_predicate_has_its_own_reason(overrides, rt, cfg, now, reason):
    assert fork.fork_eligible(_eligible_parent(**overrides), rt, cfg, now=now) == (False, reason)


def test_eligible_parent_is_eligible():
    assert fork.fork_eligible(_eligible_parent(), _RT, {}, now=_NOW) == (True, "")


def test_prefix_larger_than_child_window_is_still_eligible():
    """No prefix ceiling (Ace 2026-10-21, supersedes D-9): a parent prefix far above 0.6x the
    child's window, and a legacy ``fork_max_prefix_tokens`` knob, do not refuse the fork."""
    from agent.model_metadata import estimate_messages_tokens_rough, get_model_context_length

    window = get_model_context_length(_RT["model"])
    big = [{"role": "user", "content": "x" * (window * 4)}, {"role": "assistant", "content": "a"}]
    assert estimate_messages_tokens_rough(big) > window * 0.6
    parent = _eligible_parent(_session_messages=big)
    assert fork.fork_eligible(parent, _RT, {}, now=_NOW) == (True, "")
    assert fork.fork_eligible(parent, _RT, {"boomerang": {"fork_max_prefix_tokens": 1}}, now=_NOW) == (True, "")


def test_fork_child_over_compaction_threshold_compacts_on_first_turn():
    """A fork child whose inherited prefix + task is over its compaction threshold runs the
    same turn-loop compaction the parent would (``agent/turn_preflight.py::
    compress_after_tool_results`` -> ``ContextCompressor.should_compress`` against
    ``threshold_tokens``), on its first turn, and completes instead of erroring."""
    parent = _warm_parent()
    window = parent.context_compressor.context_length
    parent._session_messages = [
        {"role": "user", "content": "y" * (window * 3)},
        {"role": "assistant", "content": "noted"},
    ]
    child = _build(parent)
    assert child._inherit_mode == "fork"
    threshold = child.context_compressor.threshold_tokens
    assert 0 < threshold < window
    user_message, history = fork.fork_run_inputs(child, "verify the canary")

    responses = iter([
        SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", id="toolu_1", name="read_file",
                                     input={"path": "/nonexistent/fork-compaction-probe"})],
            stop_reason="tool_use", stop_sequence=None, model="claude-fable-5-1",
            usage=SimpleNamespace(input_tokens=threshold + 50_000, output_tokens=10,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        ),
        _anthropic_response("canary verified"),
    ])
    captured = []

    def _create(kwargs, **_kw):
        captured.append(json.dumps(kwargs["messages"], default=str))
        return next(responses)

    def _compress(messages, system_message, **kw):
        start = max(i for i, m in enumerate(messages) if m.get("role") == "user" and "BOOMERANG FORK" in str(m.get("content")))
        return messages[start:], child._cached_system_prompt

    with ExitStack() as stack:
        _patches(stack)
        stack.enter_context(patch.object(child, "_create_request_anthropic_client", return_value=MagicMock()))
        stack.enter_context(patch.object(child, "_anthropic_messages_create", side_effect=_create))
        mock_compress = stack.enter_context(patch.object(child, "_compress_context", side_effect=_compress))
        stack.enter_context(patch.object(child, "_persist_session"))
        stack.enter_context(patch.object(child, "_save_trajectory"))
        stack.enter_context(patch.object(child, "_cleanup_task_resources"))
        result = child.run_conversation(user_message, conversation_history=history)

    assert mock_compress.called, "fork child over threshold never compacted"
    assert result.get("failed") is not True
    assert result["completed"] is True
    assert result["final_response"] == "canary verified"
    # First request carried the full inherited prefix (warm read); the one after compaction did not.
    assert len(captured) == 2
    assert "y" * 1000 in captured[0] and "y" * 1000 not in captured[1]


def test_warm_uses_usage_split_over_cache_ttl():
    # _cache_ttl says 5m, the last response wrote 1h (apx): 10 minutes old is still warm.
    parent = _eligible_parent(_cache_ttl="5m")
    assert fork.fork_eligible(parent, _RT, {}, now=1_000_000.0 + 600) == (True, "")


def test_5m_parent_never_forks_even_fresh():
    parent = _eligible_parent(_last_cache_creation_split={"1h": 0, "5m": 10})
    assert fork.fork_eligible(parent, _RT, {}, now=1_000_000.0 + 1) == (False, "warm")


def test_ineligible_fork_runs_the_v1_fold_and_logs(caplog):
    parent = _warm_parent()
    parent.last_served_at = time.time() - 56 * 60
    caplog.set_level(logging.INFO, logger="tools.delegate_tool")
    child = _build(parent)
    assert "boomerang.fork_ineligible reason=warm" in caplog.text
    assert getattr(child, "_fork_history", None) is None
    assert "INHERITED CONTEXT FROM PARENT SESSION" in child.prefill_messages[0]["content"]
    assert getattr(child, "_first_request_extra_headers", None) is None


# ── D-16 / D-2 resolution ──────────────────────────────────────────────────


def test_branch_is_fork():
    parent = _warm_parent()
    a, b = _build(parent, mode="fork"), _build(parent, mode="branch")
    assert a._inherit_mode == b._inherit_mode == "fork"
    assert _prefix_digest(_child_first_request(a)) == _prefix_digest(_child_first_request(b))


@pytest.mark.parametrize(
    "inherit_context,inherit_mode,expected",
    [(None, None, None), (False, None, None), (True, None, "summary"), (None, "branch", "fork"),
     (True, "fork", "fork"), (False, "summary", "summary")],
)
def test_resolve_inherit_mode(inherit_context, inherit_mode, expected):
    assert fork.resolve_inherit_mode(inherit_context, inherit_mode) == expected


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        fork.resolve_inherit_mode(True, "clone")


# ── INV-1: no new params -> byte-identical construction ────────────────────


def _construction_kwargs(**extra):
    parent = _warm_parent()
    captured = {}
    real = AIAgent

    def _spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    with ExitStack() as stack:
        _patches(stack)
        stack.enter_context(patch("tools.delegate_tool._load_config", return_value={}))
        stack.enter_context(patch("run_agent.AIAgent", side_effect=_spy))
        _build_child_agent(
            task_index=0, goal="g", context=None, toolsets=None, model=None, max_iterations=5,
            task_count=1, parent_agent=parent, **extra,
        )
    drop = {"thinking_callback", "tool_progress_callback", "session_db", "credential_pool", "ephemeral_system_prompt"}
    return {k: v for k, v in captured.items() if k not in drop}


def test_no_inherit_mode_builds_identical_kwargs_to_explicit_none():
    assert _construction_kwargs() == _construction_kwargs(inherit_mode=None)


def test_inherit_context_true_without_mode_is_the_v1_fold():
    kw = _construction_kwargs(inherit_context=True)
    assert "INHERITED CONTEXT FROM PARENT SESSION" in kw["prefill_messages"][0]["content"]
    assert _construction_kwargs(inherit_context=True) == _construction_kwargs(
        inherit_context=True, inherit_mode="summary")


# ── D-17 / D-18 ────────────────────────────────────────────────────────────


def test_fork_child_cannot_fork_again(caplog):
    parent = _warm_parent()
    child = _build(parent)
    child._session_messages = parent._session_messages
    child.last_served_by, child.last_served_at = _SEAT, time.time()
    caplog.set_level(logging.INFO, logger="tools.delegate_tool")
    grandchild = _build(child)
    assert "boomerang.fork_ineligible reason=fork_depth" in caplog.text
    assert getattr(grandchild, "_inherit_mode", None) != "fork"


def test_fork_child_has_no_clarify():
    child = _build(_warm_parent())
    assert child._inherit_mode == "fork"
    assert getattr(child, "clarify_callback", None) is None
